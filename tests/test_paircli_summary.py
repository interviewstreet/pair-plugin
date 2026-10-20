import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))
SPEC = importlib.util.spec_from_file_location("paircli_summary", HOOKS_DIR / "paircli_summary.py")
assert SPEC and SPEC.loader
summary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = summary
SPEC.loader.exec_module(summary)

PR_URL = "https://github.com/acme/repo/pull/42"
HEAD = "a" * 40
GUIDE = f"""## Reading guide

Start here. Skipping a file does not mean it is correct.

### Critical

- [handler.py:10-14](https://github.com/acme/repo/blob/{HEAD}/handler.py#L10-L14) — Check when failed requests stop retrying.

### Skim through

None suggested.

### Usually safe to skip

None suggested.
"""
MARKDOWN = """# Change Summary

## Intent

Changed `handler.py` to retry failed requests.

{guide}

## Decisions

- The developer asked to stop after a few retries so requests do not wait forever.

## Verification

- Not run: `pytest`; no captured test run.
""".format(guide=GUIDE)


def evidence(tmp_path):
    repo = tmp_path / "repo"
    artifacts = repo / ".paircli" / "pr-42" / ("a" * 40)
    artifacts.mkdir(parents=True)
    for name in ("report.md", "signals.json", "snapshot.json"):
        (artifacts / name).write_text("{}")
    (artifacts / "snapshot.json").write_text(json.dumps({"pr": {
        "commits": [{"sha": HEAD}],
        "files": [{"path": "handler.py", "status": "modified", "hunks": [
            {"new_start": 10, "new_lines": 5},
        ]}],
    }}))
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
    assert "250 to 350 words" in prompt
    assert f"never exceed {summary.MAX_SUMMARY_WORDS} words" in prompt
    assert "raw-sessions/manifest.json" in prompt
    assert "silence, tool permission" in prompt
    assert "before the reviewed push" in prompt
    assert "No check results were recorded before this push." in prompt
    assert "No developer choices were recorded." in prompt
    assert "Do not include a diagram\nor Mermaid block" in prompt
    assert "## Diagram" not in prompt
    assert "do not add an Evidence section" in prompt
    assert "Do not include commit hashes" in prompt
    assert "complete changed-file list" in prompt


def test_intent_prompt_explains_objective_in_teammate_language(tmp_path):
    _, artifacts = evidence(tmp_path)
    prompt = summary.summary_prompt(PR_URL, artifacts)
    intent = prompt.split("Intent: ", 1)[1].split("\nReading guide:", 1)[0]
    assert "developer explaining the PR to a teammate" in intent
    assert "at most 45 words" in intent
    assert "Lead with the concrete problem or goal" in intent
    assert "not a list of implementation steps" in intent
    assert "do not copy its facts" in intent
    assert "Do not invent business motivation" in intent


def test_prompt_applies_simple_english_and_diff_based_priorities_to_whole_summary(tmp_path):
    _, artifacts = evidence(tmp_path)
    prompt = summary.summary_prompt(PR_URL, artifacts)
    assert "Use really simple English throughout the summary" in prompt
    assert "decisions, verification and file-reading reasons" in prompt
    assert "A filename, file type, new-file status, or change size alone" in prompt
    assert "Tests, config, schema and styling files can need close review" in prompt
    assert "An agent's\nclaim that tests passed is not an observed test result" in prompt
    assert "Do not add automated bug findings" in prompt
    assert "does not mean the file is correct" in prompt
    assert "Do not imply that unlisted files have been checked" in prompt
    assert "missing earlier context is unknown" in prompt
    for link in summary.reading_links(PR_URL, artifacts):
        assert link in prompt


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

    with pytest.raises(RuntimeError, match=f"exceeded {summary.MAX_SUMMARY_WORDS} words"):
        summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())

    assert target.read_text() == "previous valid summary"


@pytest.mark.parametrize("fence", ["```mermaid", "~~~mermaid", "```Mermaid", "``` mermaid"])
def test_mermaid_block_preserves_existing_summary(monkeypatch, tmp_path, fence):
    repo, artifacts = evidence(tmp_path)
    target = artifacts / "summary.md"
    target.write_text("previous valid summary")
    closing = "~~~" if fence.startswith("~~~") else "```"
    content = MARKDOWN.replace("## Intent\n", f"## Intent\n{fence}\nflowchart LR\nA --> B\n{closing}\n")
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: content)
    with pytest.raises(RuntimeError, match="must not include Mermaid diagrams"):
        summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())
    assert target.read_text() == "previous valid summary"


@pytest.mark.parametrize("content", [
    MARKDOWN.replace("## Decisions", "## Review Notes"),
    MARKDOWN + "\n## Extra\nUnnecessary details.\n",
    MARKDOWN.replace("## Intent", "## Verification", 1),
    MARKDOWN + "\n## Diagram\nNot needed here.\n",
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
        "- The developer asked to stop after a few retries so requests do not wait forever.", "",
    )
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: content)
    with pytest.raises(RuntimeError, match="empty section"):
        summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())


def test_docs_only_summary_needs_no_invented_diagram(monkeypatch, tmp_path):
    repo, artifacts = evidence(tmp_path)
    (artifacts / "snapshot.json").write_text("{}")
    content = """# Change Summary
## Intent
Help readers find setup instructions.
## Reading guide
Start here. Skipping a file does not mean it is correct.
Not enough detail to suggest files.
### Critical
None suggested.
### Skim through
None suggested.
### Usually safe to skip
None suggested.
## Decisions
No developer choices were recorded.
## Verification
No check results were recorded before this push.
"""
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: content)
    assert summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO()).read_text() == content


@pytest.mark.parametrize("old,new,error", [
    ("### Critical", "### Read first", "required reading groups"),
    ("### Skim through", "### Critical", "required reading groups"),
    ("None suggested.", "", "empty reading group"),
    ("Skipping a file does not mean it is correct.", "", "guide caveat"),
    ("handler.py:10-14", "handler.py:10-99", "allowed file links"),
    ("#L10-L14", "#L10-L99", "allowed file links"),
    ("/blob/" + HEAD, "/blob/" + "b" * 40, "allowed file links"),
    ("handler.py", "invented.py", "allowed file links"),
    ("https://github.com", "file://github.com", "allowed file links"),
    ("Check when failed requests stop retrying.", "", "one file link and a reason"),
    ("Check when failed requests stop retrying.", "See [another file](https://example.com).",
     "one file link and a reason"),
])
def test_invalid_guide_preserves_existing_summary(monkeypatch, tmp_path, old, new, error):
    repo, artifacts = evidence(tmp_path)
    target = artifacts / "summary.md"
    target.write_text("previous valid summary")
    content = MARKDOWN.replace(old, new)
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: content)
    with pytest.raises(RuntimeError, match=error):
        summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())
    assert target.read_text() == "previous valid summary"
    assert not list(artifacts.glob("pair-summary-*.md"))


def test_file_cannot_appear_in_two_groups(tmp_path):
    _, artifacts = evidence(tmp_path)
    duplicate = f"- [handler.py](https://github.com/acme/repo/blob/{HEAD}/handler.py) — Read the rest."
    content = MARKDOWN.replace("None suggested.", duplicate, 1)
    with pytest.raises(RuntimeError, match="repeats a file"):
        summary.validate_reading_guide(content, summary.reading_links(PR_URL, artifacts))


def test_reading_groups_must_be_inside_the_guide(monkeypatch, tmp_path):
    repo, artifacts = evidence(tmp_path)
    groups = GUIDE.split("### Critical", 1)[1]
    content = MARKDOWN.replace("### Critical" + groups, "")
    content = content.replace("## Decisions", "## Decisions\n### Critical" + groups)
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: content)
    with pytest.raises(RuntimeError, match="required reading groups"):
        summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO())


def test_guide_item_limit(tmp_path):
    _, artifacts = evidence(tmp_path)
    links = {f"[file{i}.py](https://github.com/acme/repo/blob/{HEAD}/file{i}.py)": f"file{i}.py"
             for i in range(summary.MAX_READING_ITEMS + 1)}
    bullets = "\n".join(f"- {link} — Check this changed rule." for link in links)
    content = MARKDOWN.replace(GUIDE.split("### Critical\n\n", 1)[1].split("\n\n###", 1)[0], bullets)
    with pytest.raises(RuntimeError, match="exceeded 8 files"):
        summary.validate_reading_guide(content, links)


def test_reading_links_use_captured_head_and_handle_deletions_renames_and_encoded_paths(tmp_path):
    _, artifacts = evidence(tmp_path)
    files = [
        {"path": "src/new name.py", "old_path": "src/old.py", "status": "renamed",
         "hunks": [{"new_start": 7, "new_lines": 1}, {"new_start": 20, "new_lines": 0}]},
        {"path": "src/deleted.py", "status": "deleted",
         "hunks": [{"old_start": 3, "old_lines": 7, "new_start": 0, "new_lines": 0}]},
        {"path": "schema.sql", "status": "added", "hunks": [{"new_start": 1, "new_lines": 3}]},
        {"path": "config/[prod].yaml", "status": "modified"},
    ]
    source = {"pr": {"commits": [{"sha": HEAD}], "files": files}}
    (artifacts / "snapshot.json").write_text(json.dumps(source))
    links = summary.reading_links(PR_URL, artifacts)
    assert f"[src/new name.py:7](https://github.com/acme/repo/blob/{HEAD}/src/new%20name.py#L7)" in links
    assert f"[schema.sql:1-3](https://github.com/acme/repo/blob/{HEAD}/schema.sql#L1-L3)" in links
    assert any(link.startswith("[config/\\[prod\\].yaml]") for link in links)
    deleted = next(link for link, path in links.items() if path == "src/deleted.py")
    assert deleted.startswith(f"[src/deleted.py]({PR_URL}/files#diff-")
    assert not any("old.py" in link or ":0" in link or ":20" in link for link in links)
    source["pr"]["commits"][-1]["sha"] = "b" * 40
    (artifacts / "snapshot.json").write_text(json.dumps(source))
    # Do not make line links to a head that the snapshot cannot confirm.
    assert all("/files#diff-" in link for link in summary.reading_links(PR_URL, artifacts))


def test_expanded_summary_budget_preserves_content_over_old_cap(monkeypatch, tmp_path):
    repo, artifacts = evidence(tmp_path)
    # Keep the extra text out of the strict guide format.
    content = MARKDOWN.replace("## Intent\n", "## Intent\n" + "Detail " * 260 + "\n")
    assert 300 < len(content.split()) <= summary.MAX_SUMMARY_WORDS
    monkeypatch.setattr(summary, "_run_codex", lambda *_args: content)
    assert summary.write_summary("codex", PR_URL, repo, artifacts, io.StringIO()).read_text() == content
