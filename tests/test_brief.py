import json
from pathlib import Path

import pytest

from pair import brief as brief_mod
from pair.brief import SaveError, prepare, render, save
from pair.config import load_config
from pair.diff import parse_diff
from pair.extract import extract, find_test_steps, is_test_command, pushback_hints
from pair.match import match
from pair.readers import discover, parse_all
from pair.risky import risky_hunks


def _extract(pr):
    files = parse_diff(pr.diff)
    return extract(pr, match(pr, parse_all(discover(pr.window_start)), files), files), files


def test_test_command_detection():
    assert is_test_command("FOO=1 go test ./...")
    assert is_test_command("cd app && uv run pytest -q 2>&1 | tail")
    assert find_test_steps("go vet ./... && go test ./... -v 2>&1 | grep ok") == ["go test ./... -v"]
    assert not is_test_command("cat > run.sh <<'EOF'\npytest -q\nEOF\nchmod +x run.sh")
    assert not is_test_command('echo "run pytest later"')


def test_extract_sections(demo, pr7):
    ex, _ = _extract(pr7)
    s = ex["sections"]
    assert [a["refs"][0] for a in s["ask"]] == ["claude:sess-main-1:t1", "claude:sess-sed:e1", "codex:cdx-1:turn-1"]

    ver = [(v["status"], v["text"]) for v in s["verification"]]
    # The Codex go test ran after the last code edit and failed; pytest ran before later edits.
    assert ver == [("failed", "go test ./..."), ("never_run", "Try the login page in a browser"),
                   ("stale", "pytest -q")]

    [d] = s["decisions"]
    assert d["answer"] == "Cookie" and d["options"] == ["Cookie", "Redis"] and not d["superseded"]
    assert s["churn"] == []
    assert [x["refs"] for x in s["loose_ends"]] == [["hunk:app/ui.py:1"]]

    cands = {c["ref"]: c for c in ex["pushback_candidates"]}
    assert set(cands) == {"claude:sess-main-1:t1", "claude:sess-main-1:t2", "claude:sess-main-1:t3",
                          "codex:cdx-1:turn-1", "codex:cdx-1:turn-2"}
    t2 = cands["claude:sess-main-1:t2"]
    assert t2["interrupted_before"] and t2["previous_agent_message"].startswith("Done")
    # interruption before (3) + "tested" (3) + "left"/"instead" reversal (2) + position word (1) + "?" (1) + short (1)
    assert t2["score"] == 11
    assert pushback_hints(t2["prompt"]) == ["asks about testing", "reversal words", "layout or default change"]
    assert pushback_hints("make normal mode the default") == ["layout or default change"]
    assert pushback_hints("add a --json flag") == []

    assert len(ex["plans"]) == 2
    assert "hunk:app/login.py:1" in ex["refs"] and "file:app/ui.py" in ex["refs"]
    assert "claude:sess-main-1:r1" in ex["refs"]  # question-and-answer record
    assert not any(r.startswith("claude:sess-sibling") for r in ex["refs"])


def test_risky_hunks_skip_docs(pr7):
    risky = risky_hunks(parse_diff(pr7.diff))
    assert [(h["ref"], h["tags"]) for h in risky] == [("hunk:app/cleanup.py:1", ["process", "exec"])]


def test_render_wraps_long_lines():
    text = "\n".join(render({"a": "x" * 1000, "items": [{"ref": "r1", "prompt": "line1\nline2"}], "empty": []}))
    assert max(len(line) for line in text.splitlines()) <= 410
    assert "- ref: r1" in text and "  prompt: |" in text and "empty: []" in text


@pytest.fixture
def prepared(demo, pr7, monkeypatch):
    monkeypatch.setattr(brief_mod, "fetch_pr", lambda repo, n, cfg: pr7)
    out = prepare("acme/demo#7", load_config())
    return out, Path(out["dir"])


def test_prepare_writes_brief(prepared):
    out, d = prepared
    assert out["status"] == "started"
    assert out["write"] == ["side_effects", "plan_vs_result", "pushback", "summary"]
    assert out["sessions"] == 4 and out["discussion_only"] == 1 and out["related"] == 1
    for name in ("brief.json", "evidence.json", "risky_hunks.json", "diff.patch"):
        assert (d / name).exists()
    for s in ("side_effects", "plan_vs_result", "pushback", "summary"):
        assert Path(out["inputs"][s]).exists()
    b = json.loads((d / "brief.json").read_text())
    assert b["pending"] == ["side_effects", "plan_vs_result", "pushback", "summary", "look_first"]
    assert {s["id"] for s in b["sessions"]} == {"sess-main-1", "sess-sed", "cdx-1", "sess-talk"}
    push_text = Path(out["inputs"]["pushback"]).read_text()
    assert "allowed_refs:" in push_text and "- asks about testing" in push_text


def test_prepare_reuses_finished_brief(prepared, pr7):
    out, d = prepared
    again = prepare("acme/demo#7", load_config())
    assert again["status"] == "in_progress"
    b = json.loads((d / "brief.json").read_text())
    b["pending"] = []
    (d / "brief.json").write_text(json.dumps(b))
    assert prepare("acme/demo#7", load_config())["status"] == "ready"
    assert prepare("acme/demo#7", load_config(), force=True)["status"] == "started"


def test_save_is_lenient(prepared):
    _, d = prepared
    items = [{"text": "Kills a process by pid.", "refs": ["hunk:app/cleanup.py:1", "hunk:nope:9"]},
             {"text": "Invented ref only.", "refs": ["claude:made:up"]}]
    report = save(d, "side_effects", json.dumps(items))
    assert report["kept"] == 1 and report["dropped_items"] == 1 and report["dropped_refs"] == 2
    b = json.loads((d / "brief.json").read_text())
    assert b["sections"]["side_effects"] == [{"text": "Kills a process by pid.", "refs": ["hunk:app/cleanup.py:1"]}]
    assert "side_effects" not in b["pending"]


def test_cli_save_reads_output_file(prepared, capsys):
    from pair.cli import main

    out, d = prepared
    assert out["outputs"]["side_effects"] == str(d / "out" / "side_effects.json")
    Path(out["outputs"]["side_effects"]).write_text(json.dumps([{"text": "Kills by pid.",
                                                                  "refs": ["hunk:app/cleanup.py:1"]}]))
    assert main(["save", str(d), "side_effects"]) == 0
    assert json.loads(capsys.readouterr().out)["kept"] == 1
    assert not (d / "out" / "side_effects.json").exists() and (d / "out" / "side_effects.1.json").exists()
    assert main(["save", str(d), "side_effects", "--file", "/dev/null"]) == 1
    assert "not valid JSON" in capsys.readouterr().out
    assert main(["save", str(d), "pushback", "--empty"]) == 0
    b = json.loads((d / "brief.json").read_text())
    assert b["sections"]["pushback"] == [] and "pushback" not in b["pending"]


def _write_event(path) -> str:
    return json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Write",
                       "tool_input": {"file_path": str(path), "content": "..."}, "tool_response": {}})


def test_hook_save(prepared, monkeypatch, capsys):
    import io

    from pair.cli import main

    out, d = prepared

    def hook(path, content=None):
        if content is not None:
            Path(path).write_text(content)
        monkeypatch.setattr("sys.stdin", io.StringIO(_write_event(path)))
        code = main(["hook-save"])
        return code, capsys.readouterr()

    code, cap = hook(out["outputs"]["pushback"], json.dumps([{"text": '"do it again"', "refs": ["pr:body"]}]))
    assert code == 0
    ctx = json.loads(cap.out)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("pair-brief save: ") and json.loads(ctx.split(": ", 1)[1])["kept"] == 1
    code, cap = hook(out["outputs"]["look_first"], json.dumps([{"text": "x", "refs": ["pr:body"]}]))
    assert code == 2 and "hunk: ref" in cap.err and not cap.out
    assert not Path(out["outputs"]["look_first"]).exists()
    other = d.parent.parent.parent / "elsewhere" / "k" / "sha" / "out" / "pushback.json"
    other.parent.mkdir(parents=True)
    assert hook(other, "[]") == (0, capsys.readouterr()) and other.exists()
    assert hook(d / "out" / "ask.json", "[]")[0] == 0 and (d / "out" / "ask.json").exists()


def test_save_errors(prepared):
    _, d = prepared
    with pytest.raises(SaveError, match="not valid JSON"):
        save(d, "pushback", "[{oops")
    with pytest.raises(SaveError, match="none of the 1 items"):
        save(d, "pushback", json.dumps([{"text": "x", "refs": ["claude:made:up"]}]))
    with pytest.raises(SaveError, match="hunk: ref"):
        save(d, "look_first", json.dumps([{"text": "x", "refs": ["claude:sess-main-1:t1"]}]))
    with pytest.raises(SaveError, match="unknown section"):
        save(d, "ask", "[]")
    # An empty list is a valid "nothing to report".
    assert save(d, "plan_vs_result", "[]")["kept"] == 0


def test_save_pushback_marks_superseded_decisions(prepared):
    _, d = prepared
    items = [{"text": '"put the login button on the left instead" moved the button.',
              "refs": ["claude:sess-main-1:t2"], "supersedes": ["claude:sess-main-1:r1"]}]
    report = save(d, "pushback", "```json\n" + json.dumps(items) + "\n```")
    assert report["superseded_decisions"] == 1
    b = json.loads((d / "brief.json").read_text())
    [dec] = b["sections"]["decisions"]
    assert dec["superseded"] and dec["superseded_by"] == "claude:sess-main-1:t2"


def test_supersedes_picks_the_matching_question_and_drops_other_refs():
    from pair.brief import _apply_supersedes

    shared = "claude:s:q1"
    brief = {"sections": {"decisions": [
        {"text": "Q: Where should the editor pane go? A: left of the agent pane", "refs": [shared]},
        {"text": "Q: What should the status bar show? A: repo:worktree", "refs": [shared]}]}}
    items = [{"text": '"have the agent pane to the left and the editor to the right" swapped the panes.',
              "refs": ["claude:s:p9"], "supersedes": [shared, "claude:s:p3"]},
             {"text": "unrelated", "refs": ["claude:s:p4"], "supersedes": "claude:s:p5"}]
    assert _apply_supersedes(brief, items) == 1
    pane, status = brief["sections"]["decisions"]
    assert pane["superseded_by"] == "claude:s:p9" and not status.get("superseded")
    assert items[0]["supersedes"] == [shared] and "supersedes" not in items[1]


def test_look_first_limit_and_summary_input(prepared):
    _, d = prepared
    items = [{"text": f"look {i}", "refs": ["hunk:app/login.py:1"]} for i in range(9)]
    report = save(d, "look_first", json.dumps(items))
    assert report["kept"] == 7 and report["dropped_items"] == 2
    save(d, "side_effects", json.dumps([{"text": "kills", "refs": ["hunk:app/cleanup.py:1"]}]))
    summary_in = (d / "inputs/summary.md").read_text()
    assert "kills" in summary_in and "side_effects" not in summary_in.split("still_pending:")[1].split("allowed")[0]


def test_look_first_keeps_backing_refs_and_summary_defaults_to_pr_body(prepared):
    _, d = prepared
    save(d, "look_first", json.dumps([{"text": "why", "refs": ["hunk:app/ui.py:1", "claude:sess-main-1:t1"]}]))
    save(d, "summary", json.dumps([{"text": "A login flow."}]))
    s = json.loads((d / "brief.json").read_text())["sections"]
    assert s["look_first"][0]["refs"] == ["hunk:app/ui.py:1", "claude:sess-main-1:t1"]
    assert s["summary"] == [{"text": "A login flow.", "refs": ["pr:body"]}]
