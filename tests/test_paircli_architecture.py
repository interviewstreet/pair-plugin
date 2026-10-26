import copy
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from paircli_architecture import (
    ArchitectureError, normalize_architecture, validate_architecture, write_architecture,
)
from paircli_archive import create_pr_archive
from paircli_snapshot import (
    GITHUB_COMMIT_LIST_LIMIT, MAX_NO_LINE_DATA_PATHS, files_without_line_data, lines_available,
    no_line_data_note, snapshot_binds_head,
)
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
    assert "exactly the keys id, label, nodes" in calls[0] and "no episode_ids" in calls[0]
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
    # The summary no longer runs the diagram step; the scan does, with the same harness runner.
    assert len(calls) == 1 and not (folder / "generated_artifacts/architecture.json").exists()
    generate_diagram = paircli_summary.agent_runner(harness)
    write_architecture(folder, lambda prompt: generate_diagram(prompt, tmp_path), io.StringIO())
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
    monkeypatch.setattr(paircli_summary, "_run_codex", lambda *_args: markdown)
    result = paircli_summary.write_summary("codex", "https://github.com/acme/reviews/pull/42",
                                          tmp_path, folder, io.StringIO())
    assert write_architecture(folder, lambda _: architecture_output, io.StringIO()) is None
    assert result.read_text() == markdown
    assert "## Diagram" not in result.read_text() and "```mermaid" not in result.read_text()
    with zipfile.ZipFile(create_pr_archive(tmp_path, folder)) as archive:
        assert archive.read("overview.md") == markdown.encode()
    assert not (folder / "generated_artifacts/architecture.json").exists()


@pytest.mark.parametrize("fault", [
    "stale-head", "extra-key", "duplicate-id", "bad-endpoint", "bad-flow",
    "missing-citation", "unknown-file", "invented-lines", "unbacked-file", "invented-episode",
    "boolean-layer", "missing-nodes", "snapshot-head",
])
def test_rejects_invalid_or_unsupported_graphs(fault):
    document = copy.deepcopy(diagram())
    source = snapshot()
    if fault == "stale-head": document["head_sha"] = "b" * 40
    if fault == "extra-key": document["html"] = "<script>"
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
    with pytest.raises(ArchitectureError) as caught:
        validate_architecture(document, HEAD, source)
    assert caught.value.errors and all(error["path"] and error["reason"] for error in caught.value.errors)


def test_extra_fields_are_stripped_with_warnings_and_never_written(tmp_path):
    document = diagram()
    document["html"] = "<script>"
    document["flows"][0]["episode_ids"] = []
    document["nodes"][0]["evidence"][0]["note"] = "x"
    document["edges"][0]["style"] = "dashed"
    folder = prepare(tmp_path)
    log = io.StringIO()
    result = write_architecture(folder, lambda _: json.dumps(document), log)
    assert json.loads(result.read_text()) == diagram()
    assert "<script>" not in result.read_text()
    for path in ("html", "flows[0].episode_ids", "nodes[0].evidence[0].note", "edges[0].style"):
        assert f"architecture normalized: {path}: removed unexpected key" in log.getvalue()


def test_normalize_defaults_missing_episode_ids_and_keeps_invented_ones():
    document = diagram()
    del document["nodes"][0]["episode_ids"]
    cleaned, warnings = normalize_architecture(document)
    assert cleaned["nodes"][0]["episode_ids"] == [] and warnings == []
    assert "episode_ids" not in document["nodes"][0]  # the input is not mutated
    cleaned["edges"][0]["episode_ids"] = ["ep-1"]
    with pytest.raises(ArchitectureError) as caught:
        validate_architecture(normalize_architecture(cleaned)[0], HEAD, snapshot())
    assert [error["path"] for error in caught.value.errors] == ["edges[0].episode_ids"]


@pytest.mark.parametrize("document", [None, [], "text", 7, {"nodes": "bad", "edges": 3, "flows": [1, None]}])
def test_malformed_documents_give_clean_errors(document):
    cleaned, _ = normalize_architecture(document)
    with pytest.raises(ArchitectureError) as caught:
        validate_architecture(cleaned, HEAD, snapshot())
    assert caught.value.errors


def test_validation_collects_independent_errors_with_paths():
    document = diagram()
    document["title"] = ""
    document["nodes"][0]["evidence"][0]["start"] = 500
    document["nodes"][1]["kind"] = "toaster"
    document["nodes"][1]["episode_ids"] = ["ep-1"]
    document["edges"][0]["target"] = "missing"
    document["flows"][0]["extra"] = True
    with pytest.raises(ArchitectureError) as caught:
        validate_architecture(document, HEAD, snapshot())
    errors = {error["path"]: error["reason"] for error in caught.value.errors}
    assert set(errors) >= {
        "title", "nodes[0].evidence[0].start", "nodes[1].kind", "nodes[1].episode_ids",
        "edges[0].target", "flows[0].extra",
    }
    assert "unexpected key" in errors["flows[0].extra"]
    assert "title: " in str(caught.value) and "(+" in str(caught.value)


def test_flow_with_episode_ids_is_accepted(tmp_path):
    document = diagram()
    document["flows"][0]["episode_ids"] = []
    result = write_architecture(prepare(tmp_path), lambda _: json.dumps(document), io.StringIO())
    assert json.loads(result.read_text())["flows"] == diagram()["flows"]


def test_raw_output_and_errors_are_saved_and_kept_out_of_zip(tmp_path):
    folder = prepare(tmp_path)
    document = diagram()
    document["nodes"][0]["evidence"][0]["end"] = 500
    raw = json.dumps(document)
    log = io.StringIO()
    assert write_architecture(folder, lambda _: raw, log) is None
    assert (folder / "diagnostics" / "architecture-raw.txt").read_text() == raw
    errors = json.loads((folder / "diagnostics" / "architecture-errors.json").read_text())
    assert errors[0] == {"path": "nodes[0].evidence[0].start",
                         "reason": "lines must lie inside one new-side hunk of the cited file"}
    assert "nodes[0].evidence[0].start: lines must lie" in log.getvalue()
    assert not list((folder / "diagnostics").glob("*.tmp"))
    with zipfile.ZipFile(create_pr_archive(tmp_path, folder)) as archive:
        assert not any("diagnostics" in name for name in archive.namelist())


def test_valid_run_keeps_raw_output_but_no_errors_and_clears_stale_files(tmp_path):
    folder = prepare(tmp_path)
    (folder / "diagnostics").mkdir()
    (folder / "diagnostics" / "architecture-errors.json").write_text("[]")
    (folder / "diagnostics" / "architecture-raw.txt").write_text("stale")
    raw = json.dumps(diagram())
    assert write_architecture(folder, lambda _: raw, io.StringIO()) is not None
    assert (folder / "diagnostics" / "architecture-raw.txt").read_text() == raw
    assert not (folder / "diagnostics" / "architecture-errors.json").exists()


def test_oversized_raw_output_is_truncated_with_marker(tmp_path):
    folder = prepare(tmp_path)
    assert write_architecture(folder, lambda _: "x" * (512 * 1024 + 100), io.StringIO()) is None
    saved = (folder / "diagnostics" / "architecture-raw.txt").read_bytes()
    assert len(saved) <= 512 * 1024 and saved.rstrip().endswith(b"bytes]")
    assert "[truncated" in saved.decode().splitlines()[-1]


def test_does_not_follow_diagnostics_directory_symlink(tmp_path):
    folder = prepare(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (folder / "diagnostics").symlink_to(external, target_is_directory=True)
    assert write_architecture(folder, lambda _: json.dumps(diagram()), io.StringIO()) is None
    assert list(external.iterdir()) == []
    # A symlink there must not make the whole evidence ZIP fail either.
    with zipfile.ZipFile(create_pr_archive(tmp_path, folder)) as archive:
        assert not any("diagnostics" in name for name in archive.namelist())


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


def bad_diagram():
    document = diagram()
    document["nodes"][0]["evidence"][0]["end"] = 500
    document["title"] = ""
    return document


def test_invalid_first_diagram_is_retried_once_with_the_errors(tmp_path):
    folder = prepare(tmp_path)
    prompts = []
    answers = iter([json.dumps(bad_diagram()), json.dumps(diagram())])

    def generate(prompt):
        prompts.append(prompt)
        return next(answers)

    log = io.StringIO()
    result = write_architecture(folder, generate, log)
    assert json.loads(result.read_text()) == diagram()
    assert len(prompts) == 2 and prompts[1].startswith(prompts[0])
    assert "title: must be 1-160 characters" in prompts[1]
    assert "nodes[0].evidence[0].start: lines must lie inside one new-side hunk of the cited file" in prompts[1]
    assert "corrected full JSON object" in prompts[1]
    assert "retrying once" in log.getvalue()
    diagnostics = folder / "diagnostics"
    assert (diagnostics / "architecture-raw-1.txt").read_text() == json.dumps(bad_diagram())
    assert (diagnostics / "architecture-raw.txt").read_text() == json.dumps(diagram())
    assert {error["path"] for error in json.loads((diagnostics / "architecture-errors-1.json").read_text())} >= {"title"}
    assert not (diagnostics / "architecture-errors.json").exists()


def test_retry_prompt_caps_listed_errors_at_twenty(tmp_path):
    folder = prepare(tmp_path)
    document = diagram()
    document["gaps"] = ["x"] * 3 + [""] * 3
    document["nodes"] = [dict(document["nodes"][0], id=f"n{index}", label="") for index in range(20)]
    prompts = []
    write_architecture(folder, lambda prompt: prompts.append(prompt) or json.dumps(document), io.StringIO())
    listed = prompts[1].split("(path: reason):\n", 1)[1].split("\nFix them", 1)[0].splitlines()
    assert len(prompts) == 2 and len(listed) == 20


def test_two_invalid_diagrams_log_write_diagnostics_and_return_none(tmp_path):
    folder = prepare(tmp_path)
    calls = []
    first, second = json.dumps(bad_diagram()), json.dumps({"schema_version": 2})
    answers = iter([first, second])
    log = io.StringIO()
    assert write_architecture(folder, lambda prompt: calls.append(prompt) or next(answers), log) is None
    assert len(calls) == 2
    diagnostics = folder / "diagnostics"
    assert (diagnostics / "architecture-raw-1.txt").read_text() == first
    assert (diagnostics / "architecture-raw.txt").read_text() == second
    assert (diagnostics / "architecture-errors-1.json").is_file()
    assert any(error["path"] == "schema_version"
               for error in json.loads((diagnostics / "architecture-errors.json").read_text()))
    assert "architecture unavailable (ArchitectureError" in log.getvalue()
    assert not (folder / "generated_artifacts" / "architecture.json").exists()


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("codex", 300), OSError("no harness")])
def test_timeouts_and_os_errors_are_not_retried(tmp_path, failure):
    folder = prepare(tmp_path)
    calls = []

    def generate(prompt):
        calls.append(prompt)
        raise failure

    assert write_architecture(folder, generate, io.StringIO()) is None
    assert len(calls) == 1


def test_timeout_during_the_retry_ends_without_a_third_call(tmp_path):
    folder = prepare(tmp_path)
    calls = []

    def generate(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return "not JSON"
        raise subprocess.TimeoutExpired("codex", 300)

    assert write_architecture(folder, generate, io.StringIO()) is None
    assert len(calls) == 2


def test_null_is_not_retried(tmp_path):
    folder = prepare(tmp_path)
    calls = []
    assert write_architecture(folder, lambda prompt: calls.append(prompt) or "null", io.StringIO()) is None
    assert len(calls) == 1
    assert not (folder / "diagnostics" / "architecture-raw-1.txt").exists()


def test_null_on_the_retry_is_accepted_as_no_diagram(tmp_path):
    folder = prepare(tmp_path)
    answers = iter(["not JSON", "null"])
    log = io.StringIO()
    assert write_architecture(folder, lambda _: next(answers), log) is None
    assert "no supported runtime diagram" in log.getvalue()


def commit_list(count, include_head=False):
    commits = [{"sha": f"{index:040x}"} for index in range(1, count + 1)]
    if include_head:
        commits[-1] = {"sha": HEAD}
    return commits


def test_snapshot_binds_head_rules():
    assert snapshot_binds_head({"commits": [{"sha": "b" * 40}, {"sha": HEAD}]}, HEAD)
    # A complete list must end at the head: the scan resolved B, a push added C, so [A, B, C] is another head.
    assert not snapshot_binds_head({"commits": [{"sha": HEAD}, {"sha": "b" * 40}]}, HEAD)
    assert not snapshot_binds_head({"commits": [{"sha": "a" * 40}, {"sha": HEAD}, {"sha": "c" * 40}]}, HEAD)
    assert not snapshot_binds_head({"commits": [{"sha": "b" * 40}]}, HEAD)
    for pr in ({}, {"commits": []}, {"commits": None}, {"commits": ["x", 3]}, None, []):
        assert not snapshot_binds_head(pr, HEAD)


def test_a_full_hundred_commit_list_may_end_before_the_head():
    assert GITHUB_COMMIT_LIST_LIMIT == 100
    document, source = diagram(), snapshot()
    source["pr"]["commits"] = commit_list(100)
    assert source["pr"]["commits"][-1]["sha"] != HEAD
    validate_architecture(document, HEAD, source)  # truncated by GitHub: accepted
    source["pr"]["commits"] = commit_list(99)
    with pytest.raises(ArchitectureError) as caught:  # complete list without the head: rejected
        validate_architecture(document, HEAD, source)
    assert [error["path"] for error in caught.value.errors] == ["snapshot.pr.commits"]
    source["pr"]["commits"] = commit_list(99, include_head=True)
    validate_architecture(document, HEAD, source)


def test_lines_available_needs_a_hunk():
    assert lines_available({"hunks": [{"new_start": 1, "new_lines": 1}]})
    for file in ({}, {"hunks": []}, {"hunks": None}, {"hunks": ["x"]}, None):
        assert not lines_available(file)
    pr = {"files": [
        {"path": "a.py", "hunks": [{"new_start": 1, "new_lines": 1}]},
        {"path": "big.sql"}, {"path": "logo.png", "hunks": []}, {"path": "big.sql"}, {"path": 3}, "x",
    ]}
    assert files_without_line_data(pr) == ["big.sql", "logo.png"]
    assert files_without_line_data({}) == [] and no_line_data_note({"files": pr["files"][:1]}) == ""


def test_citing_a_file_without_hunks_fails_validation():
    document, source = diagram(), snapshot()
    source["pr"]["files"][0]["hunks"] = []
    with pytest.raises(ArchitectureError) as caught:
        validate_architecture(document, HEAD, source)
    assert any("new-side hunk" in error["reason"] for error in caught.value.errors)


def test_diagram_prompt_names_files_without_line_data_with_a_cap(tmp_path):
    folder = prepare(tmp_path)
    source = snapshot()
    source["pr"]["files"] += [{"path": f"data/file{index}.bin", "status": "added"}
                              for index in range(MAX_NO_LINE_DATA_PATHS + 4)]
    (folder / "snapshot.json").write_text(json.dumps(source))
    prompts = []
    write_architecture(folder, lambda prompt: prompts.append(prompt) or "null", io.StringIO())
    prompt = prompts[0]
    assert "no line data" in prompt and "never cite them with line ranges" in prompt
    assert f'"data/file{MAX_NO_LINE_DATA_PATHS - 1}.bin"' in prompt
    assert f'"data/file{MAX_NO_LINE_DATA_PATHS}.bin"' not in prompt and "and 4 more" in prompt
    assert "src/reviews.ts\"" not in prompt.split("no line data", 1)[1]
    # With every file citable the line is absent altogether.
    (folder / "snapshot.json").write_text(json.dumps(snapshot()))
    prompts.clear()
    write_architecture(folder, lambda prompt: prompts.append(prompt) or "null", io.StringIO())
    assert "no line data" not in prompts[0]


def test_reading_links_for_files_without_hunks_are_whole_file_only(tmp_path):
    folder = prepare(tmp_path)
    source = snapshot()
    source["pr"]["files"].append({"path": "big.sql", "status": "modified", "hunks": []})
    (folder / "snapshot.json").write_text(json.dumps(source))
    entry = paircli_summary.reading_table("https://github.com/acme/reviews/pull/42", folder)["big.sql"]
    assert entry["ranges"] == [] and f"/blob/{HEAD}/big.sql)" in entry["link"]
    prompt = paircli_summary.summary_prompt("https://github.com/acme/reviews/pull/42", folder)
    assert 'no line data, so never cite them with line ranges: "big.sql".' in prompt


def test_prompts_reference_evidence_by_path_and_stay_small_for_huge_snapshots(tmp_path):
    marker = "SECRET-BODY-MARKER"
    hunk_text = "HUGE-HUNK-LINE " * 40
    files = []
    for index in range(5000):
        hunk = {"old_start": 1, "old_lines": 60, "new_start": 1, "new_lines": 60,
                "lines": [{"kind": "add", "text": f"{hunk_text}{index}"}] * 20}
        files.append({"path": f"src/module{index}/component.ts", "status": "modified",
                      **({"hunks": [hunk] * 3} if index % 2 else {})})
    source = {"pr": {"title": marker, "body": marker * 50_000, "commits": [{"sha": HEAD, "message": marker * 100}],
                     "files": files}, "sessions": [{"text": marker * 10_000}]}
    folder = tmp_path / ".paircli" / "pr-42" / HEAD
    folder.mkdir(parents=True)
    (folder / "snapshot.json").write_text(json.dumps(source))
    assert (folder / "snapshot.json").stat().st_size > 3_000_000
    for name in ("report.md", "signals.json"):
        (folder / name).write_text(marker)
    pr_url = "https://github.com/acme/reviews/pull/42"
    prompts = [paircli_summary.summary_prompt(pr_url, folder)]
    architecture_prompts = []
    write_architecture(folder, lambda prompt: architecture_prompts.append(prompt) or "null", io.StringIO())
    prompts += architecture_prompts
    # Retry prompts add only capped validator output.
    prompts.append(paircli_summary.retry_prompt(prompts[0], paircli_summary.SummaryValidationError("x" * 100_000)))
    invalid = json.dumps({"nodes": [{"id": str(index)} for index in range(40)]})
    write_architecture(folder, lambda prompt: architecture_prompts.append(prompt) or invalid, io.StringIO())
    prompts += architecture_prompts[-1:]
    assert len(prompts) == 4 and len(architecture_prompts) == 3
    for prompt in prompts:
        assert len(prompt.encode("utf-8")) < 30_000
        assert marker not in prompt and "HUGE-HUNK-LINE" not in prompt
        assert str(folder) in prompt
    assert f"{5000 - paircli_summary.MAX_LISTED_PATHS} more not listed" in prompts[0]
    assert "and 2450 more" in architecture_prompts[0]
    assert "rejected with these errors" in architecture_prompts[-1]


def test_a_timeout_on_the_retry_is_not_reported_as_no_diagram(tmp_path):
    from paircli_architecture import llm_failure_reason
    folder = prepare(tmp_path)
    answers = iter(["{}"])

    def generate(_prompt):
        try:
            return next(answers)
        except StopIteration:
            raise subprocess.TimeoutExpired("codex", 300)

    assert write_architecture(folder, generate, io.StringIO()) is None
    reason = llm_failure_reason(folder)
    assert "no diagram" not in reason and "unavailable" in reason
    # A later successful-but-null run starts clean and reports its own outcome.
    assert write_architecture(folder, lambda _: "null", io.StringIO()) is None
    assert llm_failure_reason(folder) == "llm returned no diagram"


def test_a_validation_failure_on_the_retry_still_reports_validation(tmp_path):
    from paircli_architecture import llm_failure_reason
    folder = prepare(tmp_path)
    assert write_architecture(folder, lambda _: "{}", io.StringIO()) is None
    assert llm_failure_reason(folder) == "llm output failed validation"


def test_diagram_prompt_grounds_descriptions_in_cited_lines(tmp_path):
    folder = prepare(tmp_path)
    prompts = []
    write_architecture(folder, lambda prompt: prompts.append(prompt) or "null", io.StringIO())
    assert "not on the PR title" in prompts[0] and "only if\nthe cited lines show it" in prompts[0]
