import json
import subprocess
from pathlib import Path

import pytest

from pair.diff import parse_diff
from pair.match import match
from pair.readers import discover, parse_all
from pair.repos import locate, normalize_remote, rel_path


def _matches(pr):
    sessions = parse_all(discover(pr.window_start))
    return {m.key: m for m in match(pr, sessions, parse_diff(pr.diff))}


def test_window(pr7):
    assert pr7.window_start.isoformat() == "2026-09-19T00:00:00+00:00"
    assert pr7.window_end.isoformat() == "2026-09-20T11:06:00+00:00"


def test_build_session_with_subagent_and_pr_link(demo, pr7):
    m = _matches(pr7)["claude:sess-main-1"]
    assert m.kept and m.role == "build" and m.opened_pr
    assert m.build_rules == {"edited_pr_file"}
    # The subagent's edit to app/session.py counts toward the parent session.
    assert "s1" in [t.turn_id for t in m.build_turns]
    # Context turns are main-session turns in the window, including /plan, which edited nothing.
    assert [t.turn_id for t in m.context_turns] == ["t1", "t2", "t3"]
    # 6 of 9 added lines appear in the diff; the plan file's three list items don't.
    assert m.overlap == pytest.approx(0.67, abs=0.01)


def test_codex_build_session(demo, pr7):
    m = _matches(pr7)["codex:cdx-1"]
    assert m.kept and m.role == "build" and m.overlap == 1.0
    assert not m.opened_pr


def test_head_branch_rule_catches_sed_edits(demo, pr7):
    m = _matches(pr7)["claude:sess-sed"]
    assert m.kept and m.role == "build" and m.build_rules == {"on_head_branch"}
    assert m.overlap is None


def test_discussion_only(demo, pr7):
    m = _matches(pr7)["claude:sess-talk"]
    assert m.kept and m.role == "discussion" and m.talk_rules == {"repo#n"}
    assert m.context_turns == []


def test_sibling_worktree_is_related_not_kept(demo, pr7):
    m = _matches(pr7)["claude:sess-sibling"]
    assert m.role == "build" and m.overlap == 0.0 and not m.kept


def test_pair_runs_and_imports_are_skipped(demo, pr7):
    keys = _matches(pr7)
    assert "claude:sess-pair-run" not in keys
    assert "codex:cdx-import" not in keys


def test_pr_url_does_not_match_longer_numbers(demo, pr7):
    from pair.match import mention_rule
    from pair.model import Turn
    from pair.util import ts
    t = Turn("claude", "s", "t", ts("2026-09-20T10:00:00Z"), "see https://github.com/acme/demo/pull/70 please")
    assert mention_rule(t, pr7, in_repo=False) is None
    t.prompt = "see https://github.com/acme/demo/pull/7."
    assert mention_rule(t, pr7, in_repo=False) == "url"


def test_normalize_remote():
    assert normalize_remote("git@github.com:Five-SH/awt.git\n") == "five-sh/awt"
    assert normalize_remote("https://github.com/interviewstreet/hackerrank") == "interviewstreet/hackerrank"
    assert normalize_remote("https://x-access-token:abc@github.com/o/r.git") == "o/r"


def test_locate_linked_worktree(tmp_path):
    root = tmp_path.resolve()
    main = root / "main"
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "remote", "add", "origin", "git@github.com:acme/wt.git"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "init"], check=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
    wt = root / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "--detach", str(wt)], check=True)
    from pair import repos
    repos.clear_caches()
    assert locate(str(wt / "src/a.py")) == ("acme/wt", str(wt))
    assert rel_path(str(wt / "src/a.py"), "acme/wt") == "src/a.py"
    assert rel_path(str(wt / "src/a.py"), "acme/other") is None


def test_layout_fallback_for_deleted_worktrees():
    from pair import repos
    repos.clear_caches()
    loc = locate("/Users/someone/awt/workspaces/demo/feature-x/app/login.py", "acme/demo")
    assert loc == ("acme/demo", "/Users/someone/awt/workspaces/demo/feature-x")


EXPECTED = {
    "five-sh/awt#1": {"keep": {"03747bd6", "51e2e625", "880302e3", "a49144e5", "c52ab291", "d060ef66"},
                      "absent": {"4e96d7d3", "01a0cca7"}},
    "interviewstreet/pair-be#56": {"keep": {"1b6016cd"}},
    "interviewstreet/hackerrank#39418": {"keep": {"01a08813", "01a086b4"}, "related": {"01a084fe", "01a087e0"}},
    "interviewstreet/workspace-service#757": {"keep": {"019fd7df"}, "related": {"019fd74f", "019fda3d"}},
}


@pytest.mark.local
@pytest.mark.parametrize("spec", sorted(EXPECTED))
def test_local_test_prs(spec):
    root = Path(__file__).parents[1]
    out = subprocess.run([str(root / "bin/pair-brief"), "match", spec], capture_output=True, text=True, check=True)
    d = json.loads(out.stdout)
    kept = {m["id"][:8] for m in d["kept"]}
    related = {m["id"][:8] for m in d["related"]}
    exp = EXPECTED[spec]
    assert kept == exp["keep"]
    assert exp.get("related", set()) <= related
    assert not (exp.get("absent", set()) & (kept | related))
