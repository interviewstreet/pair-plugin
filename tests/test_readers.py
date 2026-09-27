from pathlib import Path

import pytest

from pair.readers import discover, parse_all, read_any
from pair.readers.claude import read_claude
from pair.readers.codex import read_codex
from pair.util import ts


def test_claude_main_session(demo):
    s = read_claude(str(demo["claude"] / "-demo/sess-main-1.jsonl"))
    assert s.session_id == "sess-main-1"
    assert s.title == "Add login flow"
    assert s.cwd == str(demo["repo"])
    assert not s.subagent
    assert s.pr_links == ["https://github.com/acme/demo/pull/7"]
    assert s.first_command == "/model sonnet"
    assert not s.is_pair_run

    # The caveat, /model, and the compact summary don't start turns; /plan with arguments does.
    assert [t.turn_id for t in s.turns] == ["t1", "t2", "t3"]
    t1, t2, t3 = s.turns
    assert t3.prompt == "/plan tidy the README"
    assert t1.branch == "feature/login-flow"
    assert t1.interrupted and not t2.interrupted

    [q] = t1.questions
    assert q.question == "Where should sessions be stored?"
    assert q.options == ["Cookie", "Redis"]
    assert q.answer == "Cookie"
    assert q.ref == "claude:sess-main-1:r1"

    assert [p.path for p in t1.plans] == [str(demo["repo"] / "docs/plan-login.md"), None]

    paths = [Path(e.path).name for e in t1.edits]
    assert paths == ["plan-login.md", "login.py", "middleware.py", "cookies.py"]
    login = next(e for e in t1.edits if e.path.endswith("login.py"))
    assert "    session = create_session(request.user)" in login.added

    [test_run] = t1.commands
    assert test_run.cmd.endswith("pytest -q") and test_run.output == "3 passed" and not test_run.failed
    assert [Path(e.path).name for e in t2.edits] == ["ui.py"]
    assert t2.edits[0].added == ["LOGIN_BUTTON_POSITION = 'left'"]
    assert t2.commands[0].output.startswith("https://github.com/acme/demo/pull/7")
    assert t1.assistant_text[-1].startswith("Done")
    assert t1.messages == []


def test_claude_subagent_counts_toward_parent(demo):
    s = read_claude(str(demo["claude"] / "-demo/sess-main-1/subagents/agent-abc123.jsonl"))
    assert s.session_id == "sess-main-1"
    assert s.subagent
    [t] = s.turns
    assert t.subagent
    assert t.edits[0].path == str(demo["repo"] / "app/session.py")


def test_pair_runs_are_flagged(demo):
    s = read_claude(str(demo["claude"] / "-demo/sess-pair-run.jsonl"))
    assert s.is_pair_run
    assert s.turns[0].prompt == "/pair:pair acme/demo#7"


def test_codex_session(demo):
    s = read_codex(str(demo["codex"] / "2026/09/20/rollout-2026-09-20T11-00-00-cdx1.jsonl"))
    assert (s.agent, s.session_id, s.cwd) == ("codex", "cdx-1", str(demo["repo"]))
    assert [t.turn_id for t in s.turns] == ["turn-1", "turn-2"]
    t1, t2 = s.turns
    assert t1.branch == "feature/login-flow"
    assert t1.interrupted and not t2.interrupted
    assert t1.commands[0].cmd == "go test ./..." and t1.commands[0].failed
    assert t1.edits[0].added == ["    check_rate_limit(request.remote_addr)"]
    assert t1.assistant_text == ["Added rate limiting."]
    assert t1.ref == "codex:cdx-1:turn-1"


def test_codex_imports_are_skipped(demo):
    assert read_codex(str(demo["codex"] / "2026/09/20/rollout-2026-09-20T12-00-00-import.jsonl")) is None


def test_messages_mode_keeps_transcript(demo):
    s = read_any(str(demo["claude"] / "-demo/sess-main-1.jsonl"), messages=True)
    roles = [m.role for m in s.turns[0].messages]
    assert roles[0] == "user" and "assistant" in roles and "tool" in roles
    assert any("Answer: Cookie" in m.text for m in s.turns[0].messages)
    c = read_any(str(demo["codex"] / "2026/09/20/rollout-2026-09-20T11-00-00-cdx1.jsonl"), messages=True)
    assert any(m.text.startswith("Bash: go test") for m in c.turns[0].messages)


def test_discover_and_parse_all(demo):
    paths = discover(ts("2026-01-01T00:00:00+00:00"))
    assert len(paths) == 8
    sessions = parse_all(paths)
    assert sorted(s.key for s in sessions) == [
        "claude:sess-main-1", "claude:sess-main-1", "claude:sess-pair-run", "claude:sess-sed", "claude:sess-sibling",
        "claude:sess-talk", "codex:cdx-1"]


D060 = Path.home() / ".claude/projects/-Users-kousik-dev-awt/d060ef66-24dc-4a46-ad58-0409007c28bf.jsonl"


@pytest.mark.local
@pytest.mark.skipif(not D060.exists(), reason="awt build session not on this machine")
def test_local_awt_build_session():
    s = read_claude(str(D060))
    assert len(s.turns) == 7
    answers = [q for t in s.turns for q in t.questions if q.answer]
    assert len(answers) == 4
    assert any(e.path.endswith("internal/tmux/tmux.go") for t in s.turns for e in t.edits)
