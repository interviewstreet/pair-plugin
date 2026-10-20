import copy
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from paircli_architecture import validate_architecture, write_architecture
from paircli_archive import create_pr_archive
import paircli_summary

HEAD = "a" * 40
READING_GUIDE = f"""## Reading guide
Start here. Skipping a file does not mean it is correct.
### Critical
- [src/reviews.ts:10-29](https://github.com/acme/reviews/blob/{HEAD}/src/reviews.ts#L10-L29) — Check which reviews can be saved.
### Skim through
None suggested.
### Usually safe to skip
None suggested.
"""


def diagram():
    evidence = [{"type": "diff", "path": "src/reviews.ts", "start": 12, "end": 25}]
    return {
        "schema_version": 1, "head_sha": HEAD,
        "title": "Persist reviews", "description": "The API saves reviews.",
        "nodes": [
            {"id": "api", "label": "Review API", "kind": "service", "layer": 0,
             "change": "modified", "description": "Validate and save reviews.",
             "files": ["src/reviews.ts"], "episode_ids": [], "evidence": evidence},
            {"id": "db", "label": "PostgreSQL", "kind": "database", "layer": 1,
             "change": "unchanged", "description": "Persist reviews.",
             "files": [], "episode_ids": [], "evidence": evidence},
        ],
        "edges": [{"id": "save", "source": "api", "target": "db", "label": "Insert",
                   "change": "modified", "description": "Insert validated review.",
                   "episode_ids": [], "evidence": evidence}],
        "flows": [{"id": "submit", "label": "Submission", "nodes": ["api", "db"]}],
        "gaps": [],
    }


def snapshot():
    return {"pr": {"commits": [{"sha": HEAD}], "files": [
        {"path": "src/reviews.ts", "hunks": [{"new_start": 10, "new_lines": 20}]},
    ]}}


def prepare(tmp_path):
    folder = tmp_path / ".paircli" / "pr-42" / HEAD
    folder.mkdir(parents=True)
    (folder / "snapshot.json").write_text(json.dumps(snapshot()))
    (folder / "summary.md").write_text("# Change Summary\nSample evidence.")
    return folder


def test_generates_and_archives_ade_root_artifact(tmp_path):
    folder = prepare(tmp_path)
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return json.dumps(diagram())

    result = write_architecture(folder, generate, io.StringIO())
    assert json.loads(result.read_text()) == diagram()
    assert HEAD in calls[0] and "episode_ids empty" in calls[0]
    with zipfile.ZipFile(create_pr_archive(tmp_path, folder)) as archive:
        assert json.loads(archive.read("generated_artifacts/architecture.json")) == diagram()
        assert f".paircli/pr-42/{HEAD}/generated_artifacts/architecture.json" in archive.namelist()
        assert archive.read("overview.md").startswith(b"# Change Summary")


@pytest.mark.parametrize("harness,runner", [("codex", "_run_codex"), ("claude-code", "_run_claude")])
def test_summary_then_architecture_use_triggering_harness(tmp_path, monkeypatch, harness, runner):
    folder = prepare(tmp_path)
    for name in ("report.md", "signals.json"):
        (folder / name).write_text("{}")
    markdown = "# Change Summary\n" + "\n".join(
        READING_GUIDE if section == "## Reading guide" else section + "\nRecorded evidence.\n"
        for section in paircli_summary.REQUIRED_SECTIONS
    )
    responses = iter([markdown, json.dumps(diagram())])
    calls = []

    def generate(prompt, repo):
        calls.append((prompt, repo))
        return next(responses)

    monkeypatch.setattr(paircli_summary, runner, generate)
    result = paircli_summary.write_summary(harness, "https://github.com/acme/reviews/pull/42",
                                          tmp_path, folder, io.StringIO())
    assert result.read_text() == markdown.strip() + "\n"
    assert "## Diagram" not in result.read_text()
    assert "## Intent" in result.read_text() and "## Decisions" in result.read_text()
    assert READING_GUIDE in result.read_text()
    assert len(calls) == 2 and all(repo == tmp_path for _, repo in calls)
    assert json.loads((folder / "generated_artifacts/architecture.json").read_text()) == diagram()
    with zipfile.ZipFile(create_pr_archive(tmp_path, folder)) as archive:
        assert b"## Diagram" not in archive.read("overview.md")
        assert READING_GUIDE.encode() in archive.read("overview.md")
        assert "generated_artifacts/architecture.json" in archive.namelist()


@pytest.mark.parametrize("architecture_output", ["null", "bad JSON"])
def test_summary_stays_without_diagram_when_board_unavailable(tmp_path, monkeypatch, architecture_output):
    folder = prepare(tmp_path)
    for name in ("report.md", "signals.json"):
        (folder / name).write_text("{}")
    markdown = ("# Change Summary\n## Intent\nPersist reviews.\n" + READING_GUIDE
                + "## Decisions\nNo developer choices were recorded.\n## Verification\nNo check results were recorded before this push.\n")
    responses = iter([markdown, architecture_output])
    monkeypatch.setattr(paircli_summary, "_run_codex", lambda *_args: next(responses))
    result = paircli_summary.write_summary("codex", "https://github.com/acme/reviews/pull/42",
                                          tmp_path, folder, io.StringIO())
    assert result.read_text() == markdown
    assert "## Diagram" not in result.read_text() and "```mermaid" not in result.read_text()
    with zipfile.ZipFile(create_pr_archive(tmp_path, folder)) as archive:
        assert archive.read("overview.md") == markdown.encode()
    assert not (folder / "generated_artifacts/architecture.json").exists()


@pytest.mark.parametrize("fault", [
    "stale-head", "extra-field", "duplicate-id", "bad-endpoint", "bad-flow",
    "missing-citation", "unknown-file", "invented-lines", "unbacked-file", "invented-episode",
    "boolean-layer", "missing-nodes", "snapshot-head",
])
def test_rejects_invalid_or_unsupported_graphs(fault):
    document = copy.deepcopy(diagram())
    source = snapshot()
    if fault == "stale-head": document["head_sha"] = "b" * 40
    if fault == "extra-field": document["html"] = "<script>"
    if fault == "duplicate-id": document["nodes"][1]["id"] = "api"
    if fault == "bad-endpoint": document["edges"][0]["target"] = "missing"
    if fault == "bad-flow": document["flows"][0]["nodes"].reverse()
    if fault == "missing-citation": document["nodes"][0]["evidence"] = []
    if fault == "unknown-file": document["nodes"][0]["evidence"][0]["path"] = "secret.ts"
    if fault == "invented-lines": document["nodes"][0]["evidence"][0]["end"] = 500
    if fault == "unbacked-file": document["nodes"][0]["files"] = ["unknown.ts"]
    if fault == "invented-episode": document["nodes"][0]["episode_ids"] = ["ep-1"]
    if fault == "boolean-layer": document["nodes"][0]["layer"] = True
    if fault == "missing-nodes": document["nodes"] = []
    if fault == "snapshot-head": source["pr"]["commits"][-1]["sha"] = "b" * 40
    with pytest.raises(ValueError):
        validate_architecture(document, HEAD, source)


@pytest.mark.parametrize("output", ["null", "not JSON", '{"head_sha":"wrong"}', "x" * (512 * 1024 + 1)])
def test_absent_or_invalid_generation_removes_stale_diagram(tmp_path, output):
    folder = prepare(tmp_path)
    target = folder / "generated_artifacts" / "architecture.json"
    target.parent.mkdir()
    target.write_text("previous")
    assert write_architecture(folder, lambda _: output, io.StringIO()) is None
    assert not target.exists()
    assert (folder / "summary.md").exists()


def test_timeout_is_optional_and_does_not_block_summary(tmp_path):
    folder = prepare(tmp_path)

    def generate(_prompt):
        raise subprocess.TimeoutExpired("codex", 300)

    log = io.StringIO()
    assert write_architecture(folder, generate, log) is None
    assert "keeping Markdown summary" in log.getvalue()


def test_does_not_follow_generated_directory_symlink(tmp_path):
    folder = prepare(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "architecture.json"
    sentinel.write_text("untouched")
    (folder / "generated_artifacts").symlink_to(external, target_is_directory=True)
    assert write_architecture(folder, lambda _: json.dumps(diagram()), io.StringIO()) is None
    assert sentinel.read_text() == "untouched"
