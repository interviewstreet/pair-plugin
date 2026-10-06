import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
SPEC = importlib.util.spec_from_file_location("paircli_summary", HOOKS_DIR / "paircli_summary.py")
assert SPEC and SPEC.loader
summary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = summary
SPEC.loader.exec_module(summary)

PR_URL = "https://github.com/acme/repo/pull/42"
MARKDOWN = """# PR summary

## What changed

Changed `handler.py` to retry failed requests.

## Flowchart

```mermaid
flowchart LR
  request --> retry --> response
```

## Verification steps

Run `pytest`; no captured test run.

## Risks and open questions

Retry limits need review.

## Evidence

`snapshot.json` and `report.md`.
"""


def evidence(tmp_path):
    repo = tmp_path / "repo"
    artifacts = repo / ".paircli" / "pr-42" / ("a" * 40)
    artifacts.mkdir(parents=True)
    for name in ("report.md", "signals.json", "snapshot.json"):
        (artifacts / name).write_text("{}")
    return repo, artifacts


def test_codex_writes_ai_summary_atomically(monkeypatch, tmp_path):
    repo, artifacts = evidence(tmp_path)
    monkeypatch.setattr(summary, "_executable", lambda name: "/bin/codex")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[command.index("--output-last-message") + 1]).write_text(MARKDOWN)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(summary.subprocess, "run", fake_run)

    result = summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())

    command, options = calls[0]
    assert command[:2] == ["/bin/codex", "exec"]
    assert command[command.index("-s") + 1] == "read-only"
    assert "--ephemeral" in command and "--ignore-user-config" in command
    assert str(artifacts) in options["input"]
    assert result.read_text() == MARKDOWN
    assert not list(artifacts.glob("pair-summary-*.md"))


def test_claude_writes_ai_summary_without_edit_tools(monkeypatch, tmp_path):
    repo, artifacts = evidence(tmp_path)
    monkeypatch.setattr(summary, "_executable", lambda name: "/bin/claude")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=MARKDOWN)

    monkeypatch.setattr(summary.subprocess, "run", fake_run)

    result = summary.write_summary("claude-code", PR_URL, repo, artifacts, io.StringIO())

    command, options = calls[0]
    assert "--restricted" in command and "Read,Glob,Grep" in command
    assert options["cwd"] == repo
    assert result.read_text() == MARKDOWN


def test_invalid_ai_output_does_not_replace_existing_summary(monkeypatch, tmp_path):
    repo, artifacts = evidence(tmp_path)
    target = artifacts / "summary.md"
    target.write_text("previous valid summary")
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: "no evidence")

    with pytest.raises(RuntimeError, match="required Markdown sections"):
        summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())

    assert target.read_text() == "previous valid summary"
