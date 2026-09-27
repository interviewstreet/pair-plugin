import re
from dataclasses import dataclass, field

from pair.config import pair_home
from pair.diff import FileDiff, added_lines
from pair.github import PR
from pair.model import Session, Turn
from pair.repos import dir_in_repo, rel_path

MIN_OVERLAP = 0.15
MIN_LINE = 12


@dataclass
class Match:
    session: Session
    parts: list[Session]
    role: str  # build or discussion
    opened_pr: bool
    build_turns: list[Turn]
    talk_turns: list[Turn]
    context_turns: list[Turn]
    build_rules: set[str] = field(default_factory=set)
    talk_rules: set[str] = field(default_factory=set)
    overlap: float | None = None
    kept: bool = True

    @property
    def key(self) -> str:
        return self.session.key

    @property
    def all_turns(self) -> list[Turn]:
        seen = {t.ref: t for t in self.context_turns + self.build_turns + self.talk_turns}
        return sorted(seen.values(), key=lambda t: t.started)

    @property
    def title(self) -> str:
        if self.session.title:
            return self.session.title
        first = next((t for t in self.all_turns if not t.subagent), None)
        return (first.prompt.strip().splitlines() or [""])[0][:80] if first else ""

    def summary(self) -> dict:
        turns = self.all_turns
        return {
            "agent": self.session.agent, "id": self.session.session_id, "role": self.role,
            "opened_pr": self.opened_pr, "title": self.title, "cwd": self.session.cwd,
            "started_at": turns[0].started.isoformat() if turns else None,
            "ended_at": turns[-1].started.isoformat() if turns else None,
            "paths": [p.path for p in self.parts],
            "build_turns": len(self.build_turns), "talk_turns": len(self.talk_turns),
            "context_turns": len(self.context_turns), "build_rules": sorted(self.build_rules),
            "talk_rules": sorted(self.talk_rules), "overlap": self.overlap,
            "branches": sorted({t.branch for t in turns if t.branch}),
        }


def _url_rx(pr: PR) -> re.Pattern:
    return re.compile(re.escape(pr.url) + r"(?!\d)", re.I)


def mention_rule(t: Turn, pr: PR, in_repo: bool) -> str | None:
    hay = t.prompt + "\n" + "\n".join(c.cmd for c in t.commands)
    name = re.escape(pr.repo.split("/")[1])
    n = pr.number
    if _url_rx(pr).search(hay):
        return "url"
    if re.search(rf"\b{name}(#|/pulls?/){n}\b", hay, re.I):
        return "repo#n"
    branch = pr.head_branch
    if len(branch) >= 8 and branch not in ("main", "master") and re.search(rf"(?<![\w/-]){re.escape(branch)}(?![\w/-])", hay):
        return "branch"
    if in_repo and re.search(rf"(?<![\w/])#{n}\b|/pulls?/{n}\b", hay):
        return "#n_in_repo"
    return None


def overlap(pr: PR, turns: list[Turn], pr_added: set[str]) -> float | None:
    """Share of a session's added lines (in edits to PR files) that also appear in the PR diff."""
    pr_files = {f["path"] for f in pr.files}
    lines = {ln.strip() for t in turns for e in t.edits if rel_path(e.path, pr.repo) in pr_files
             for ln in e.added if len(ln.strip()) >= MIN_LINE}
    return round(len(lines & pr_added) / len(lines), 2) if lines else None


def keep(m: Match) -> bool:
    if m.role != "build" or m.opened_pr or "on_head_branch" in m.build_rules:
        return True
    return m.overlap is None or m.overlap >= MIN_OVERLAP


def _skip(main: Session) -> bool:
    home = str(pair_home())
    return main.is_pair_run or bool(main.cwd and main.cwd.startswith(home))


def match(pr: PR, sessions: list[Session], diff_files: list[FileDiff]) -> list[Match]:
    """Every session with a build or discussion turn for the PR. `kept` is False for weak build matches."""
    pr_files = {f["path"] for f in pr.files} | {f.path for f in diff_files}
    pr_added = added_lines(diff_files, MIN_LINE)
    since, until = pr.window_start, pr.window_end
    url_rx = _url_rx(pr)

    grouped: dict[str, list[Session]] = {}
    for s in sessions:
        grouped.setdefault(s.key, []).append(s)

    out = []
    for parts in grouped.values():
        main = next((p for p in parts if not p.subagent), parts[0])
        if _skip(main):
            continue
        in_repo = dir_in_repo(main.cwd, pr.repo)
        build, talk, build_rules, talk_rules = [], [], set(), set()
        for s in parts:
            for t in s.turns:
                in_window = since <= t.started <= until
                touched = {rel for e in t.edits if (rel := rel_path(e.path, pr.repo))}
                if in_window and touched & pr_files:
                    build.append(t)
                    build_rules.add("edited_pr_file")
                elif in_window and in_repo and t.branch == pr.head_branch and (t.edits or t.commands):
                    build.append(t)
                    build_rules.add("on_head_branch")
                elif t.started >= since and (rule := mention_rule(t, pr, in_repo)):
                    talk.append(t)
                    talk_rules.add(rule)
        if not (build or talk):
            continue
        context = sorted((t for s in parts if not s.subagent for t in s.turns if since <= t.started <= until),
                         key=lambda t: t.started) if build else []
        opened = any(url_rx.search(u) for p in parts for u in p.pr_links) or any(
            url_rx.search(c.output) for p in parts for t in p.turns for c in t.commands)
        m = Match(main, parts, "build" if build else "discussion", opened, sorted(build, key=lambda t: t.started),
                  sorted(talk, key=lambda t: t.started), context, build_rules, talk_rules)
        m.overlap = overlap(pr, m.build_turns, pr_added)
        m.kept = keep(m)
        out.append(m)
    return sorted(out, key=lambda m: (not m.kept, m.role != "build", m.all_turns[0].started if m.all_turns else 0))
