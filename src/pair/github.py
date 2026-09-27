import concurrent.futures as cf
import functools
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pair.config import Config
from pair.util import ts

VIEW_FIELDS = ("number,url,title,body,state,author,headRefName,baseRefName,headRefOid,baseRefOid,files,commits,"
               "additions,deletions,createdAt,mergedAt")


class GhError(RuntimeError):
    pass


@functools.cache
def _token(account: str) -> str | None:
    try:
        p = subprocess.run(["gh", "auth", "token", "--user", account], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() or None


def gh_env(repo: str, cfg: Config) -> dict[str, str]:
    """Environment for gh calls on `repo`, with GH_TOKEN set from the account configured for its owner."""
    env = os.environ.copy()
    account = cfg.account_for(repo)
    if account and (tok := _token(account)):
        env["GH_TOKEN"] = tok
    return env


def gh(args: list[str], env: dict[str, str], stdin: str | None = None, timeout: int = 60) -> str:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, env=env, input=stdin, timeout=timeout)
    if p.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])} failed: {(p.stderr or p.stdout).strip()[-500:]}")
    return p.stdout


@dataclass
class PR:
    repo: str
    number: int
    url: str
    title: str
    body: str
    state: str
    author: str
    head_branch: str
    base_branch: str
    head_oid: str
    base_oid: str
    files: list[dict]
    commits: list[dict]
    merge_base: str
    merge_base_time: datetime
    diff: str
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def head_commit_time(self) -> datetime:
        return max(ts(c["committedDate"]) for c in self.commits)

    @property
    def first_authored(self) -> datetime:
        return min(ts(c["authoredDate"]) for c in self.commits)

    @property
    def window_start(self) -> datetime:
        """Rebases move the merge base forward, so also reach back to a day before the first authored commit."""
        return min(self.merge_base_time, self.first_authored - timedelta(hours=24))

    @property
    def window_end(self) -> datetime:
        return self.head_commit_time + timedelta(hours=1)

    def basics(self) -> dict:
        return {"repo": self.repo, "number": self.number, "title": self.title, "url": self.url,
                "head_branch": self.head_branch, "base_branch": self.base_branch, "body": self.body,
                "state": self.state, "author": self.author, "head_sha": self.head_oid, "merge_base": self.merge_base}


def _timed(fn, *args):
    t = time.perf_counter()
    out = fn(*args)
    return out, round(time.perf_counter() - t, 3)


def fetch_pr(repo: str, number: int, cfg: Config) -> PR:
    env = gh_env(repo, cfg)
    timings: dict[str, float] = {}
    with cf.ThreadPoolExecutor(3) as ex:
        f_view = ex.submit(_timed, gh, ["pr", "view", str(number), "-R", repo, "--json", VIEW_FIELDS], env)
        f_diff = ex.submit(_timed, gh, ["pr", "diff", str(number), "-R", repo], env)
        view_raw, timings["gh_pr_view"] = f_view.result()
        view = json.loads(view_raw)
        f_mb = ex.submit(_timed, gh, ["api", f"repos/{repo}/compare/{view['baseRefOid']}...{view['headRefOid']}",
                                      "--jq", "{sha: .merge_base_commit.sha, date: .merge_base_commit.commit.committer.date}"],
                         env)
        mb_raw, timings["gh_merge_base"] = f_mb.result()
        try:
            diff, timings["gh_pr_diff"] = f_diff.result()
        except GhError as e:
            diff = _local_diff(repo, number, json.loads(mb_raw)["sha"], view["headRefOid"])
            if diff is None:
                raise GhError(f"{e}; no local clone to diff from") from e
    mb = json.loads(mb_raw)
    return PR(repo.lower(), number, view["url"], view["title"], view.get("body") or "", view.get("state") or "",
              (view.get("author") or {}).get("login") or "", view["headRefName"], view["baseRefName"],
              view["headRefOid"], view["baseRefOid"],
              [{"path": f["path"], "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)}
               for f in view.get("files") or []],
              view.get("commits") or [], mb["sha"], ts(mb["date"]), diff, timings)


def _local_diff(repo: str, number: int, base: str, head: str) -> str | None:
    """GitHub refuses very large diffs; fall back to a local clone."""
    from pair.repos import find_clone
    clone = find_clone(repo)
    if not clone:
        return None
    subprocess.run(["git", "-C", clone, "fetch", "--quiet", "origin", f"pull/{number}/head"], capture_output=True)
    p = subprocess.run(["git", "-C", clone, "diff", f"{base}...{head}"], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def fetch_comments(repo: str, number: int, cfg: Config) -> dict:
    """Review comments on lines, conversation comments, and review summaries."""
    env = gh_env(repo, cfg)
    with cf.ThreadPoolExecutor(3) as ex:
        f_line = ex.submit(_pages, f"repos/{repo}/pulls/{number}/comments", env)
        f_issue = ex.submit(_pages, f"repos/{repo}/issues/{number}/comments", env)
        f_reviews = ex.submit(_pages, f"repos/{repo}/pulls/{number}/reviews", env)
        line, issue, reviews = f_line.result(), f_issue.result(), f_reviews.result()
    return {
        "line_comments": [{"path": c.get("path"), "line": c.get("line") or c.get("original_line"),
                           "side": c.get("side"), "author": (c.get("user") or {}).get("login"),
                           "body": c.get("body"), "created_at": c.get("created_at")} for c in line],
        "comments": [{"author": (c.get("user") or {}).get("login"), "body": c.get("body"),
                      "created_at": c.get("created_at")} for c in issue],
        "reviews": [{"author": (r.get("user") or {}).get("login"), "state": r.get("state"), "body": r.get("body"),
                     "submitted_at": r.get("submitted_at")} for r in reviews if r.get("body") or r.get("state")],
    }


def _pages(endpoint: str, env: dict[str, str]) -> list[dict]:
    pages = json.loads(gh(["api", "--paginate", "--slurp", endpoint], env) or "[]")
    return [x for page in pages for x in (page if isinstance(page, list) else [page])]


def post_review(repo: str, number: int, commit_id: str, comments: list[dict], body: str, cfg: Config) -> str:
    payload = {"commit_id": commit_id, "event": "COMMENT", "body": body, "comments": comments}
    out = gh(["api", "-X", "POST", f"repos/{repo}/pulls/{number}/reviews", "--input", "-"], gh_env(repo, cfg),
             stdin=json.dumps(payload))
    return json.loads(out).get("html_url") or ""
