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
MARKDOWN = """# Change Summary

## Intent

Changed `handler.py` to retry failed requests.

## Diagram

```mermaid
flowchart LR
  request --> retry --> response
```

## Decisions

- Builder requested bounded retries rather than retrying indefinitely.

## Verification

- Not run: `pytest`; no captured test run.
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


def test_summary_prompt_requests_concise_reviewer_content(tmp_path):
    _, artifacts = evidence(tmp_path)

    prompt = summary.summary_prompt(PR_URL, artifacts)

    for heading in ("# Change Summary", *summary.REQUIRED_SECTIONS):
        assert heading in prompt
    assert "150 to 200 words" in prompt
    assert "raw-sessions/manifest.json" in prompt
    assert "silence, tool permission" in prompt
    assert "before the reviewed push" in prompt
    assert "No pre-push verification captured." in prompt
    assert "No explicit builder decisions captured." in prompt
    assert "No runtime architecture change." in prompt
    assert "do not add an Evidence section" in prompt
    assert "Do not include commit hashes" in prompt
    assert "complete changed-file list" in prompt


def test_invalid_ai_output_does_not_replace_existing_summary(monkeypatch, tmp_path):
    repo, artifacts = evidence(tmp_path)
    target = artifacts / "summary.md"
    target.write_text("previous valid summary")
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: "no evidence")

    with pytest.raises(RuntimeError, match="required Markdown sections"):
        summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())

    assert target.read_text() == "previous valid summary"


def test_overlong_ai_output_does_not_replace_existing_summary(monkeypatch, tmp_path):
    repo, artifacts = evidence(tmp_path)
    target = artifacts / "summary.md"
    target.write_text("previous valid summary")
    overlong = MARKDOWN + (" detail" * summary.MAX_SUMMARY_WORDS)
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: overlong)

    with pytest.raises(RuntimeError, match="exceeded 300 words"):
        summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())

    assert target.read_text() == "previous valid summary"


@pytest.mark.parametrize("content", [
    MARKDOWN.replace("## Decisions", "## Review Notes"),
    MARKDOWN + "\n## Extra\nUnnecessary details.\n",
    MARKDOWN.replace("## Intent", "## Diagram", 1),
])
def test_rejects_wrong_duplicate_or_extra_sections(monkeypatch, tmp_path, content):
    repo, artifacts = evidence(tmp_path)
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: content)
    with pytest.raises(RuntimeError, match="required Markdown sections"):
        summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())
    assert not (artifacts / "summary.md").exists()


def test_rejects_empty_section(monkeypatch, tmp_path):
    repo, artifacts = evidence(tmp_path)
    content = MARKDOWN.replace(
        "- Builder requested bounded retries rather than retrying indefinitely.", "",
    )
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: content)
    with pytest.raises(RuntimeError, match="empty section"):
        summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())


def test_docs_only_summary_needs_no_invented_diagram(monkeypatch, tmp_path):
    repo, artifacts = evidence(tmp_path)
    content = """# Change Summary
## Intent
Help readers find setup instructions.
## Diagram
No runtime architecture change.
## Decisions
No explicit builder decisions captured.
## Verification
No pre-push verification captured.
"""
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: content)
    assert summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO()).read_text() == content
