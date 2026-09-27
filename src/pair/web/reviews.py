import subprocess
import threading
import time
import uuid
from functools import cached_property
from pathlib import Path

from pair.brief import latest_dir, load_json, locked, now_iso
from pair.config import Config, briefs_dir, worktrees_dir
from pair.diff import FileDiff, parse_diff
from pair.github import post_review
from pair.repos import find_clone
from pair.util import dumps, parse_review_key, write_atomic

DRAFT_STATUSES = {"draft", "approved", "discarded"}
GITHUB_SIDE = {"additions": "RIGHT", "deletions": "LEFT"}


class ReviewNotFound(KeyError):
    pass


class ReviewError(RuntimeError):
    pass


def _git(clone: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", clone, *args], capture_output=True, text=True, timeout=timeout)


class Review:
    """One PR's stored brief, evidence, diff, drafts, and checkout."""

    _fetch_lock = threading.Lock()

    def __init__(self, key: str, cfg: Config):
        self.key = key
        self.cfg = cfg
        self.repo, self.number = parse_review_key(key)
        d = latest_dir(key)
        if d is None:
            raise ReviewNotFound(key)
        self.dir = d
        self._fetched = False

    # ------------------------------------------------------------ stored files

    @cached_property
    def evidence(self) -> dict:
        return load_json(self.dir / "evidence.json", {})

    @property
    def brief(self) -> dict:
        return load_json(self.dir / "brief.json", {})

    @property
    def pr(self) -> dict:
        return self.evidence.get("pr", {})

    @property
    def head_sha(self) -> str:
        return self.dir.name

    @cached_property
    def diff_text(self) -> str:
        return (self.dir / "diff.patch").read_text()

    @cached_property
    def diff_files(self) -> dict[str, FileDiff]:
        return {f.path: f for f in parse_diff(self.diff_text)}

    @property
    def risky(self) -> list:
        return load_json(self.dir / "risky_hunks.json", [])

    @property
    def can_post(self) -> bool:
        return self.cfg.can_post(self.repo)

    def meta(self) -> dict:
        pr = self.pr
        return {
            "pr": {k: pr.get(k) for k in ("repo", "number", "title", "url", "head_branch", "base_branch", "body")},
            "head_sha": self.head_sha,
            "can_post": self.can_post,
            "post_account": self.cfg.account_for(self.repo) if self.can_post else None,
            "sessions": [{k: s.get(k) for k in ("agent", "id", "role", "opened_pr", "title", "started_at")}
                         for s in self.evidence.get("sessions", [])],
            "files": [{k: f.get(k) for k in ("path", "additions", "deletions")} for f in self.evidence.get("files", [])],
        }

    def file_diff_text(self, path: str) -> str | None:
        """The part of diff.patch for one file."""
        chunks = self.diff_text.split("\ndiff --git ")
        for i, c in enumerate(chunks):
            body = c if i == 0 else "diff --git " + c
            head = body.split("\n", 1)[0]
            if head.endswith(f" b/{path}") or head.startswith(f"diff --git a/{path} "):
                return body
        return None

    # ------------------------------------------------------------ drafts

    @property
    def _drafts_path(self) -> Path:
        return self.dir / "drafts.json"

    def drafts(self) -> list[dict]:
        return load_json(self._drafts_path, [])

    def _write_drafts(self, drafts: list[dict]) -> None:
        write_atomic(self._drafts_path, dumps(drafts))

    def check_line(self, path: str, side: str, line: int, start_line: int | None = None) -> None:
        f = self.diff_files.get(path)
        if f is None:
            raise ReviewError(f"{path} isn't in this PR's diff")
        if side not in GITHUB_SIDE:
            raise ReviewError("side must be 'additions' or 'deletions'")
        for ln in filter(None, (start_line, line)):
            if not any(h.covers(side, ln) for h in f.hunks):
                shown = ", ".join(f"{h.target_start}-{h.target_start + h.target_len - 1}" if side == "additions"
                                  else f"{h.source_start}-{h.source_start + h.source_len - 1}" for h in f.hunks)
                raise ReviewError(f"line {ln} of {path} ({side}) isn't shown in the diff; hunks cover {shown}")
        if start_line and start_line > line:
            raise ReviewError("start_line must not be after line")

    def add_draft(self, path: str, side: str, line: int, body: str, start_line: int | None = None,
                  status: str = "draft", author: str = "reviewer") -> dict:
        """Save a comment for review. The pair agent's drafts start as `draft`; the user's own start `approved`."""
        self.check_line(path, side, line, start_line)
        if not body.strip():
            raise ReviewError("the comment is empty")
        if status not in ("draft", "approved"):
            raise ReviewError("a new comment's status must be 'draft' or 'approved'")
        draft = {"id": uuid.uuid4().hex[:12], "path": path, "side": side, "line": line,
                 "start_line": start_line if start_line and start_line != line else None, "body": body.strip(),
                 "status": status, "author": author, "created_at": now_iso()}
        with locked(self.dir):
            drafts = self.drafts()
            drafts.append(draft)
            self._write_drafts(drafts)
        return draft

    def update_draft(self, draft_id: str, body: str | None = None, status: str | None = None) -> dict:
        if status is not None and status not in DRAFT_STATUSES:
            raise ReviewError(f"status must be one of {sorted(DRAFT_STATUSES)}")
        with locked(self.dir):
            drafts = self.drafts()
            d = next((x for x in drafts if x["id"] == draft_id), None)
            if d is None:
                raise ReviewNotFound(draft_id)
            if d["status"] == "posted":
                raise ReviewError("this draft was already posted")
            if body is not None:
                d["body"] = body.strip()
            if status is not None:
                d["status"] = status
            d["updated_at"] = now_iso()
            self._write_drafts(drafts)
        return d

    def post(self) -> str:
        if not self.can_post:
            raise PermissionError(f"posting to {self.repo} isn't allowed; add it to post_comments.allowed_repos")
        with locked(self.dir):
            drafts = self.drafts()
            approved = [d for d in drafts if d["status"] == "approved"]
            if not approved:
                raise ReviewError("no approved drafts to post")
            comments = []
            for d in approved:
                c = {"path": d["path"], "line": d["line"], "side": GITHUB_SIDE[d["side"]], "body": d["body"]}
                if d.get("start_line"):
                    c["start_line"], c["start_side"] = d["start_line"], GITHUB_SIDE[d["side"]]
                comments.append(c)
            url = post_review(self.repo, self.number, self.head_sha, comments, "", self.cfg)
            for d in approved:
                d["status"], d["posted_at"], d["review_url"] = "posted", now_iso(), url
            self._write_drafts(drafts)
        return url

    # ------------------------------------------------------------ git

    @cached_property
    def clone(self) -> str | None:
        return find_clone(self.repo)

    def _fetch(self) -> str:
        clone = self.clone
        if not clone:
            raise ReviewError(f"no local clone of {self.repo} in ~/dev, ~/awt/workspaces, or ~/orca/workspaces")
        with self._fetch_lock:
            if not self._fetched:
                if _git(clone, "cat-file", "-e", f"{self.head_sha}^{{commit}}").returncode != 0:
                    _git(clone, "fetch", "--quiet", "origin", f"pull/{self.number}/head")
                mb = self.pr.get("merge_base")
                if mb and _git(clone, "cat-file", "-e", f"{mb}^{{commit}}").returncode != 0:
                    _git(clone, "fetch", "--quiet", "origin", self.pr.get("base_branch") or "HEAD")
                self._fetched = True
        return clone

    def file_text(self, side: str, path: str) -> str:
        clone = self._fetch()
        rev = self.pr.get("merge_base") if side == "base" else self.head_sha
        p = _git(clone, "show", f"{rev}:{path}", timeout=30)
        if p.returncode != 0:
            raise ReviewNotFound(f"{path} at {side}")
        return p.stdout

    @property
    def worktree_path(self) -> Path:
        return worktrees_dir() / self.key

    def ensure_worktree(self) -> tuple[Path, dict]:
        t0 = time.perf_counter()
        clone = self._fetch()
        wt = self.worktree_path
        marks = {"fetch_s": round(time.perf_counter() - t0, 2)}
        t1 = time.perf_counter()
        if wt.exists() and (wt / ".git").exists():
            head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True)
            if head.stdout.strip() != self.head_sha:
                subprocess.run(["git", "-C", str(wt), "checkout", "--quiet", "--detach", self.head_sha],
                               capture_output=True)
        else:
            wt.parent.mkdir(parents=True, exist_ok=True)
            _git(clone, "worktree", "prune")
            p = _git(clone, "worktree", "add", "--quiet", "--detach", str(wt), self.head_sha)
            if p.returncode != 0:
                raise ReviewError(f"git worktree add failed: {p.stderr.strip()[-300:]}")
        marks["worktree_s"] = round(time.perf_counter() - t1, 2)
        return wt, marks

    def remove_worktree(self) -> bool:
        wt = self.worktree_path
        if not wt.exists():
            return False
        if self.clone:
            _git(self.clone, "worktree", "remove", "--force", str(wt))
            _git(self.clone, "worktree", "prune")
        return not wt.exists()


def list_reviews() -> list[dict]:
    out = []
    root = briefs_dir()
    for kd in sorted(root.glob("*")) if root.exists() else []:
        d = latest_dir(kd.name)
        if not d:
            continue
        b = load_json(d / "brief.json", {})
        pr = b.get("pr", {})
        out.append({"key": kd.name, "repo": pr.get("repo"), "number": pr.get("number"), "title": pr.get("title"),
                    "head_sha": d.name, "pending_count": len(b.get("pending", [])),
                    "updated_at": b.get("updated_at")})
    return sorted(out, key=lambda r: r.get("updated_at") or "", reverse=True)
