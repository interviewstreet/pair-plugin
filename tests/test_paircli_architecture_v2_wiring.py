"""scan()-level tests for the schema-v2 diagram step: it runs after v1 and can never fail the scan."""
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import paircli_onboarding as onboarding
from paircli_architecture_llm import generate_v2
from paircli_architecture_v2 import finalize_v2, validate_v2
from paircli_architecture_v2_draft import deterministic_v2_draft
from paircli_skeleton import build_skeleton
from snapshot_factory import HEAD, deletion_heavy_pr, make_file, make_snapshot, source_lines

PR_URL = "https://github.com/acme/repo/pull/42"
V2 = "generated_artifacts/architecture.v2.json"
V1 = "generated_artifacts/architecture.json"
GROUP_PROMPT = "You are improving the text of ONE group"
MERGE_PROMPT = "You are connecting the groups"
OVERVIEW_PROMPT = "drawing a one-screen map of a pull request"


def code_snapshot():
    """Files in several directories, with a real import line, so the draft has groups, rows and an edge."""
    return make_snapshot([
        make_file("src/api/reviews.ts", "modified",
                  added=['import { save } from "../db/store";', *source_lines(3)], removed=["old line"], start=10),
        make_file("src/db/store.ts", "modified", added=source_lines(4), removed=["old store"], start=20),
        make_file("src/lib/names.ts", "added", added=source_lines(5), start=1),
        make_file("package-lock.json", "modified", added=["a"], removed=["b"], start=1),
    ])


def docs_snapshot():
    return make_snapshot([make_file("README.md", "modified", added=["More words."], start=1)])


def digest_of(prompt):
    path = re.search(r"only source of ids: (\S+)", prompt).group(1)
    return json.loads(Path(path).read_text())


class Agent:
    """A stand-in for the harness: records v2 calls and answers each kind of prompt."""

    def __init__(self, group="patch", merge="answer", overview="answer"):
        self.group, self.merge, self.overview = group, merge, overview
        self.v2_calls, self.other_calls, self.overview_calls = [], [], []

    def __call__(self, prompt, repo_dir, timeout=None, model=None):
        if OVERVIEW_PROMPT in prompt:
            self.overview_calls.append({"cwd": repo_dir, "timeout": timeout, "model": model})
            if isinstance(self.overview, BaseException):
                raise self.overview
            if self.overview == "garbage":
                return "this is not json"
            if self.overview == "null":
                return "null"
            return self.overview_answer(prompt)
        if GROUP_PROMPT in prompt or MERGE_PROMPT in prompt:
            kind = "group" if GROUP_PROMPT in prompt else "merge"
            self.v2_calls.append({"kind": kind, "cwd": repo_dir, "timeout": timeout, "model": model})
            mode = self.group if kind == "group" else self.merge
            if isinstance(mode, BaseException):
                raise mode
            if mode == "garbage":
                return "this is not json"
            return self.answer(kind, prompt)
        self.other_calls.append(repo_dir)
        return "null"

    @staticmethod
    def overview_answer(prompt):
        """A valid overview: one component per file row of the digest, one edge and one step."""
        digest = json.loads(Path(re.search(r"node ids: (\S+)", prompt).group(1)).read_text())
        rows = [row for row in digest["nodes"] if row["kind"] == "file" and row.get("evidence")]
        components = [{"id": "c-" + row["id"], "label": "LLM " + row["label"][:30], "group": "all", "kind": "component",
                       "description": "Changed by this pull request.", "members": [row["id"]]} for row in rows]
        refs = {"c-" + row["id"]: next(ref for ref in row["evidence"] if ref["side"] == "new") for row in rows}
        first, second = components[0]["id"], components[1]["id"]
        return json.dumps({
            "groups": [{"id": "all", "label": "Everything"}], "components": components,
            "edges": [{"id": "o-call", "source": first, "target": second, "label": "call",
                       "claim": "The first part calls the second.", "evidence": [refs[first]]}],
            "steps": [{"id": "o-s1", "when": "after", "edge": "o-call", "components": [first, second],
                       "text": "The first part now calls the second.", "evidence": [refs[first]]}]})

    @staticmethod
    def answer(kind, prompt):
        digest = digest_of(prompt)
        if kind == "group":
            row = digest["rows"][0]
            return json.dumps({"rows": [{"id": row["id"], "label": "LLM " + row["label"][:60],
                                         "description": "Changed by this pull request."}],
                               "summary": "This group changes code."})
        row = next(row for group in digest["groups"] for row in group["rows"] if row.get("evidence"))
        return json.dumps({"title": "A story about the change", "steps": [
            {"id": "s1", "when": "after", "nodes": [row["id"]], "text": "This file now behaves differently.",
             "evidence": row["evidence"][:1]}]})


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("PAIRCLI_ARCHITECTURE_V2", "PAIRCLI_ARCH_LLM_GROUPS", "PAIRCLI_ARCH_CONCURRENCY",
                 "PAIRCLI_ARCH_CALL_TIMEOUT", "PAIRCLI_ARCH_DEADLINE", "PAIRCLI_MODEL_GROUP",
                 "PAIRCLI_MODEL_MERGE", "PAIRCLI_MODEL_SUMMARY", "PAIRCLI_ARCH_OVERVIEW",
                 "PAIRCLI_MODEL_OVERVIEW"):
        monkeypatch.delenv(name, raising=False)


def prepare(monkeypatch, tmp_path, snapshot=None, agent=None):
    repo = tmp_path / "repo"
    artifacts = repo / ".paircli" / "pr-42" / HEAD
    artifacts.mkdir(parents=True)
    for name in ("report.md", "signals.json"):
        (artifacts / name).write_text("{}")
    (artifacts / "snapshot.json").write_text(json.dumps(snapshot or code_snapshot()))
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.delenv("PAIRCLI_UPLOAD_ENABLED", raising=False)
    monkeypatch.setattr(onboarding, "artifact_directory", lambda *_: artifacts)
    monkeypatch.setattr(onboarding, "run_command", lambda *args, **kwargs: None)
    uploads = []
    monkeypatch.setattr(onboarding, "upload_archive",
                        lambda url, archive, auth: uploads.append(zipfile.ZipFile(archive)) or "artifact-1")
    monkeypatch.setattr(onboarding, "notify_scan_result", lambda *args, **kwargs: None)
    agent = agent or Agent()
    monkeypatch.setitem(onboarding.agent_runner.__globals__, "_run_codex", agent)
    return repo, artifacts, uploads, agent


def run_scan(tmp_path, repo, harness="codex"):
    log = tmp_path / "scan.log"
    assert onboarding.scan(log, PR_URL, repo, harness) == 0, log.read_text()
    return log.read_text()


def status_of(artifacts):
    return json.loads((artifacts / "diagnostics" / "architecture-v2-status.json").read_text())


def uploaded_v2(uploads):
    return json.loads(uploads[0].read(V2))


def assert_valid(document, snapshot=None):
    validate_v2(document, HEAD, snapshot or code_snapshot())


def test_valid_llm_output_uploads_both_v1_and_v2_at_the_zip_root(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    log = run_scan(tmp_path, repo)
    names = uploads[0].namelist()
    assert V1 in names and V2 in names
    assert f".paircli/pr-42/{HEAD}/{V2}" in names
    assert not any("diagnostics" in name for name in names)
    document = uploaded_v2(uploads)
    assert document["schema_version"] == 2 and document["generated_by"] == "llm"
    assert document["steps"] and any(node["label"].startswith("LLM ") for node in document["nodes"])
    assert_valid(document)
    assert json.loads(uploads[0].read(V1))["schema_version"] == 1
    status = status_of(artifacts)
    assert status["tier"] == "llm" and status["generated_by"] == "llm"
    assert status["counts"]["steps"] == len(document["steps"]) >= 1
    assert status["reason"].startswith("llm groups ok: ") and "merge ok" in status["reason"]
    assert status["llm_calls"]["merge"] == "ok" and status["llm_calls"]["groups_llm_ok"] >= 1
    assert re.search(r"architecture v2: tier=llm groups_llm_ok=\d+/\d+ merge=ok steps_dropped=0 cache_hits=0", log)
    assert "architecture v2 diagram: produced" in log
    assert agent.v2_calls and all(call["cwd"] == repo for call in agent.v2_calls)


@pytest.mark.parametrize("group,merge", [
    ("garbage", "garbage"),
    (subprocess.TimeoutExpired("codex", 5), subprocess.TimeoutExpired("codex", 5)),
    (RuntimeError("boom"), RuntimeError("boom")),
])
def test_model_garbage_timeout_or_crash_writes_the_deterministic_v2(monkeypatch, tmp_path, group, merge):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path, agent=Agent(group=group, merge=merge))
    log = run_scan(tmp_path, repo)
    document = uploaded_v2(uploads)
    assert document["generated_by"] == "deterministic" and document["steps"] == []
    assert_valid(document)
    assert V1 in uploads[0].namelist()
    status = status_of(artifacts)
    assert status["tier"] == "deterministic" and status["reason"] == "no llm output applied"
    assert "architecture v2: tier=deterministic" in log


def test_v2_crash_leaves_no_v2_file_but_v1_and_the_upload_complete(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)

    def explode(*_args):
        raise RuntimeError("draft builder bug")

    monkeypatch.setattr(onboarding, "deterministic_v2_draft", explode)
    log = run_scan(tmp_path, repo)
    names = uploads[0].namelist()
    assert V1 in names and V2 not in names and f".paircli/pr-42/{HEAD}/{V2}" not in names
    assert status_of(artifacts)["tier"] == "none"
    assert status_of(artifacts)["reason"] == "v2 error: RuntimeError"
    assert "architecture v2 failed (RuntimeError: draft builder bug)" in log
    assert "architecture v2 diagram: not produced" in log
    assert agent.v2_calls == []


def test_generate_v2_crash_falls_back_to_the_deterministic_document(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("generate bug")

    monkeypatch.setattr(onboarding, "generate_v2", explode)
    run_scan(tmp_path, repo)
    assert uploaded_v2(uploads)["generated_by"] == "deterministic"
    assert status_of(artifacts)["reason"] == "v2 error: RuntimeError"


def test_a_final_validation_failure_falls_back_to_the_deterministic_document(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    calls = []

    def reject_first(document, head, snapshot):
        calls.append(document["generated_by"])
        if len(calls) == 1:
            raise ValueError("late rejection")
        return validate_v2(document, head, snapshot)

    monkeypatch.setattr(onboarding, "validate_v2", reject_first)
    run_scan(tmp_path, repo)
    assert uploaded_v2(uploads)["generated_by"] == "deterministic"


def test_when_even_the_deterministic_fallback_fails_there_is_no_v2_and_the_scan_completes(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    stale = artifacts / "generated_artifacts" / "architecture.v2.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")

    def explode(*_args, **_kwargs):
        raise RuntimeError("write bug")

    monkeypatch.setattr(onboarding, "write_architecture_v2", explode)
    run_scan(tmp_path, repo)
    assert V2 not in uploads[0].namelist() and not stale.exists()
    assert status_of(artifacts)["tier"] == "none"


def test_a_crash_while_recording_the_failure_is_swallowed(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("everything is broken")

    for name in ("deterministic_v2_draft", "write_no_architecture_v2", "remove_stale_architecture_v2"):
        monkeypatch.setattr(onboarding, name, explode)
    run_scan(tmp_path, repo)
    assert V1 in uploads[0].namelist() and V2 not in uploads[0].namelist()


def test_the_step_itself_never_raises_even_with_a_broken_log_or_folder(tmp_path):
    class BrokenLog:
        def write(self, _message):
            raise OSError("closed")

    assert onboarding.run_diagram_v2_step("codex", tmp_path, tmp_path / "missing" / HEAD, BrokenLog()) is None
    assert onboarding.run_diagram_v2_step("codex", tmp_path, tmp_path / "missing" / HEAD, io.StringIO()) is None


def test_docs_only_changes_have_no_v2_file_and_tier_none(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path, snapshot=docs_snapshot())
    log = run_scan(tmp_path, repo)
    assert V2 not in uploads[0].namelist()
    assert status_of(artifacts)["tier"] == "none" and status_of(artifacts)["reason"] == "docs-only change"
    assert agent.v2_calls == []
    assert "architecture v2: tier=none reason=docs-only change" in log


def test_the_environment_switch_skips_v2_entirely_and_removes_old_output(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("PAIRCLI_ARCHITECTURE_V2", "0")
    stale = artifacts / "generated_artifacts" / "architecture.v2.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    (artifacts / "diagnostics").mkdir()
    (artifacts / "diagnostics" / "architecture-v2-status.json").write_text("{}")
    log = run_scan(tmp_path, repo)
    assert agent.v2_calls == []
    assert V2 not in uploads[0].namelist() and V1 in uploads[0].namelist()
    assert not stale.exists() and not (artifacts / "diagnostics" / "architecture-v2-status.json").exists()
    assert not (artifacts / "diagnostics" / "architecture-llm").exists()
    assert log.count("disabled by PAIRCLI_ARCHITECTURE_V2=0") == 1


def test_an_unsupported_harness_writes_the_deterministic_v2(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    log = run_scan(tmp_path, repo, harness="other")
    document = uploaded_v2(uploads)
    assert document["generated_by"] == "deterministic"
    assert status_of(artifacts)["reason"] == "llm unavailable"
    assert "no model available" in log and agent.v2_calls == []


def test_a_stale_v2_is_removed_when_the_new_run_produces_none(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path, snapshot=docs_snapshot())
    stale = artifacts / "generated_artifacts" / "architecture.v2.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"schema_version": 2}')
    run_scan(tmp_path, repo)
    assert not stale.exists() and V2 not in uploads[0].namelist()


def test_an_unreadable_snapshot_gives_no_v2(tmp_path):
    artifacts = tmp_path / ".paircli" / "pr-42" / HEAD
    artifacts.mkdir(parents=True)
    (artifacts / "snapshot.json").write_text("[not json")
    stale = artifacts / "generated_artifacts" / "architecture.v2.json"
    stale.parent.mkdir()
    stale.write_text("{}")
    assert onboarding.run_diagram_v2_step("codex", tmp_path, artifacts, io.StringIO()) is None
    assert not stale.exists() and status_of(artifacts)["reason"] == "snapshot unreadable"


def test_v1_failing_does_not_prevent_v2(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("v1 bug")

    monkeypatch.setattr(onboarding, "write_architecture", explode)
    monkeypatch.setattr(onboarding, "write_fallback_architecture", explode)
    run_scan(tmp_path, repo)
    names = uploads[0].namelist()
    assert V1 not in names and V2 in names
    assert uploaded_v2(uploads)["generated_by"] == "llm"


def test_v2_runs_after_v1_and_does_not_change_it(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    order = []
    real_v1, real_v2 = onboarding.run_diagram_step, onboarding.run_diagram_v2_step
    monkeypatch.setattr(onboarding, "run_diagram_step", lambda *a: order.append("v1") or real_v1(*a))
    monkeypatch.setattr(onboarding, "run_diagram_v2_step", lambda *a: order.append("v2") or real_v2(*a))
    run_scan(tmp_path, repo)
    first = uploads[0].read(V1)

    monkeypatch.setenv("PAIRCLI_ARCHITECTURE_V2", "0")
    run_scan(tmp_path, repo)
    assert order == ["v1", "v2", "v1", "v2"]
    assert uploads[1].read(V1) == first


def test_no_model_is_hardcoded(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    run_scan(tmp_path, repo)
    assert agent.v2_calls and {call["model"] for call in agent.v2_calls} == {None}
    assert all(call["timeout"] and call["timeout"] > 0 for call in agent.v2_calls)


def test_stage_models_come_from_the_environment(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("PAIRCLI_MODEL_GROUP", "group-model")
    monkeypatch.setenv("PAIRCLI_MODEL_MERGE", "merge-model")
    run_scan(tmp_path, repo)
    models = {call["kind"]: call["model"] for call in agent.v2_calls}
    assert models == {"group": "group-model", "merge": "merge-model"}


def test_the_whole_step_respects_the_deadline(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("PAIRCLI_ARCH_DEADLINE", "1")
    monkeypatch.setenv("PAIRCLI_ARCH_CALL_TIMEOUT", "1")
    run_scan(tmp_path, repo)
    assert uploaded_v2(uploads)["schema_version"] == 2


def test_the_model_cache_stays_local(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    run_scan(tmp_path, repo)
    assert (repo / ".paircli" / "cache" / "architecture-v2").is_dir()
    assert not any("cache" in name for name in uploads[0].namelist())


def test_a_subdirectory_scan_writes_v2_under_the_toplevel(monkeypatch, tmp_path):
    toplevel = tmp_path.resolve() / "monorepo"
    subdirectory = toplevel / "services" / "api"
    subdirectory.mkdir(parents=True)
    real_run = subprocess.run
    real_run(["git", "init", "-q", str(toplevel)], check=True)

    def fake_run(command, **kwargs):
        if command[0] == "gh":
            return subprocess.CompletedProcess(command, 0, stdout=HEAD + "\n", stderr="")
        return real_run(command, **kwargs)

    def fake_paircli(command, *, cwd, log, timeout=None):
        if command[1] == "scan":
            out = Path(command[command.index("--out") + 1])
            out.mkdir(parents=True)
            for name in ("report.md", "signals.json"):
                (out / name).write_text("{}")
            (out / "snapshot.json").write_text(json.dumps(code_snapshot()))

    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.delenv("PAIRCLI_UPLOAD_ENABLED", raising=False)
    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)
    monkeypatch.setattr(onboarding, "run_command", fake_paircli)
    uploads = []
    monkeypatch.setattr(onboarding, "upload_archive",
                        lambda url, archive, auth: uploads.append(zipfile.ZipFile(archive)) or "artifact-1")
    monkeypatch.setattr(onboarding, "notify_scan_result", lambda *args, **kwargs: None)
    agent = Agent()
    monkeypatch.setitem(onboarding.agent_runner.__globals__, "_run_codex", agent)

    log = tmp_path / "scan.log"
    assert onboarding.scan(log, PR_URL, subdirectory, "codex") == 0, log.read_text()
    artifacts = toplevel / ".paircli" / "pr-42" / HEAD
    assert (artifacts / "generated_artifacts" / "architecture.v2.json").is_file()
    assert not (subdirectory / ".paircli").exists()
    assert agent.v2_calls and all(call["cwd"] == toplevel for call in agent.v2_calls)
    assert V2 in uploads[0].namelist() and V1 in uploads[0].namelist()
    assert (toplevel / ".paircli" / "cache" / "architecture-v2").is_dir()


def test_end_to_end_with_the_real_draft_and_generate_v2_and_a_stubbed_model(tmp_path):
    snapshot = code_snapshot()
    artifacts = tmp_path / ".paircli" / "pr-42" / HEAD
    artifacts.mkdir(parents=True)
    (artifacts / "snapshot.json").write_text(json.dumps(snapshot))
    skeleton = build_skeleton(snapshot, HEAD)
    draft = deterministic_v2_draft(skeleton, snapshot, HEAD)
    assert draft is not None and draft["nodes"]
    deterministic, _ = finalize_v2(draft, HEAD, snapshot, "deterministic")

    def generate(prompt, *, timeout, model):
        kind = "group" if GROUP_PROMPT in prompt else "merge"
        assert MERGE_PROMPT in prompt or kind == "group"
        return Agent.answer(kind, prompt)

    result = generate_v2(artifacts, snapshot, HEAD, draft, generate, io.StringIO(), harness="codex",
                         skeleton=skeleton, min_call_seconds=0)
    document = result["document"]
    validate_v2(document, HEAD, snapshot)
    assert document["generated_by"] == "llm" and document["head_sha"] == HEAD
    assert len(document["steps"]) >= 1
    assert result["stats"]["merge"] == "ok" and result["stats"]["groups_llm_ok"] >= 1
    assert {node["id"] for node in document["nodes"]} == {node["id"] for node in deterministic["nodes"]}
    assert document["title"] == "A story about the change"


def test_a_deletion_heavy_scan_uploads_a_short_valid_v2_with_pattern_rows(monkeypatch, tmp_path):
    snapshot = make_snapshot(deletion_heavy_pr(templates=40, features=9))
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path, snapshot=snapshot)
    run_scan(tmp_path, repo)
    document = uploaded_v2(uploads)
    assert_valid(document, snapshot)
    kinds = sorted(node["kind"] for node in document["nodes"])
    assert kinds == ["file", "pattern", "pattern"] and len(document["edges"]) == 2
    assert sorted(node["pattern"]["count"] for node in document["nodes"] if node["kind"] == "pattern") == [9, 40]
    labels = {node["label"] for node in document["nodes"] if node["kind"] == "pattern"}
    assert all(label.startswith("LLM ") or label in {"40 files: deleted", "9 files: same edit"} for label in labels)
    assert status_of(artifacts)["counts"]["nodes"] == 3


def test_the_llm_narrates_pattern_rows_without_changing_their_members(tmp_path):
    snapshot = make_snapshot(deletion_heavy_pr(templates=12, features=5))
    artifacts = tmp_path / ".paircli" / "pr-42" / HEAD
    artifacts.mkdir(parents=True)
    skeleton = build_skeleton(snapshot, HEAD)
    draft = deterministic_v2_draft(skeleton, snapshot, HEAD)
    before = {node["id"]: node["pattern"] for node in draft["nodes"] if node["kind"] == "pattern"}
    assert len(before) == 2

    def generate(prompt, *, timeout, model):
        kind = "group" if GROUP_PROMPT in prompt else "merge"
        if kind == "group":
            digest = digest_of(prompt)
            row = next((row for row in digest["rows"] if row["kind"] == "pattern"), digest["rows"][0])
            return json.dumps({"rows": [{"id": row["id"], "label": "LLM " + row["label"][:60],
                                         "description": "Rewritten by the model.", "kind": "service"}]})
        return Agent.answer(kind, prompt)

    result = generate_v2(artifacts, snapshot, HEAD, draft, generate, io.StringIO(), harness="codex",
                         skeleton=skeleton, min_call_seconds=0)
    document = result["document"]
    validate_v2(document, HEAD, snapshot)
    after = {node["id"]: node["pattern"] for node in document["nodes"] if node["kind"] == "pattern"}
    assert after == before
    assert any(node["label"].startswith("LLM ") and node["kind"] == "pattern" for node in document["nodes"])


# --- the overview layer ----------------------------------------------------------------------

def assert_overview_status(artifacts, document, source, retried=False):
    status = status_of(artifacts)["overview"]
    overview = document["overview"]
    assert status == {"source": source, "retried": retried, "components": len(overview["components"]),
                      "edges": len(overview["edges"]), "steps": len(overview["steps"]),
                      "steps_dropped": status["steps_dropped"]}
    assert overview["generated_by"] == source


def detailed_part(document):
    return {key: value for key, value in document.items() if key != "overview"}


def test_a_valid_llm_overview_is_attached_validated_and_recorded(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    log = run_scan(tmp_path, repo)
    document = uploaded_v2(uploads)
    assert_valid(document)
    assert document["generated_by"] == "llm" and document["overview"]["generated_by"] == "llm"
    assert all(item["label"].startswith("LLM ") for item in document["overview"]["components"] if item["id"] != "other")
    assert document["overview"]["steps"] and document["overview"]["edges"]
    assert_overview_status(artifacts, document, "llm")
    assert len(agent.overview_calls) == 1 and agent.overview_calls[0]["cwd"] == repo
    assert agent.overview_calls[0]["model"] is None and 0 < agent.overview_calls[0]["timeout"] <= 120
    assert re.search(r"architecture v2: overview source=llm components=\d+ edges=1 steps=1 steps_dropped=0", log)
    assert list(document)[-1] == "overview"


def test_the_overview_leaves_the_detailed_document_untouched(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    run_scan(tmp_path, repo)
    monkeypatch.setenv("PAIRCLI_ARCH_OVERVIEW", "0")
    run_scan(tmp_path, repo)
    with_overview, without = uploaded_v2(uploads), json.loads(uploads[1].read(V2))
    assert "overview" in with_overview and "overview" not in without
    assert detailed_part(with_overview) == without


def test_the_overview_model_and_time_budget(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("PAIRCLI_MODEL_OVERVIEW", "overview-model")
    seen = []
    real = onboarding.generate_overview
    monkeypatch.setattr(onboarding, "generate_overview",
                        lambda *args, **kwargs: seen.append(kwargs) or real(*args, **kwargs))
    run_scan(tmp_path, repo)
    assert seen[0]["model"] == "overview-model" and agent.overview_calls[0]["model"] == "overview-model"
    assert 20 <= seen[0]["deadline_seconds"] <= 120

    monkeypatch.setenv("PAIRCLI_ARCH_DEADLINE", "1")
    run_scan(tmp_path, repo)
    assert seen[1]["deadline_seconds"] == 20

    monkeypatch.setenv("PAIRCLI_ARCH_DEADLINE", "3600")
    run_scan(tmp_path, repo)
    assert seen[2]["deadline_seconds"] == 120


@pytest.mark.parametrize("overview,retried", [
    ("garbage", True),
    (subprocess.TimeoutExpired("codex", 5), False),
    (RuntimeError("boom"), False),
])
def test_a_failing_overview_call_attaches_the_deterministic_overview(monkeypatch, tmp_path, overview, retried):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path, agent=Agent(overview=overview))
    run_scan(tmp_path, repo)
    document = uploaded_v2(uploads)
    assert_valid(document)
    assert document["generated_by"] == "llm", "the detailed diagram keeps its model text"
    assert document["overview"]["generated_by"] == "deterministic" and document["overview"]["steps"] == []
    assert_overview_status(artifacts, document, "deterministic", retried=retried)
    assert V1 in uploads[0].namelist()


def test_a_null_overview_answer_attaches_the_deterministic_overview(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path, agent=Agent(overview="null"))
    run_scan(tmp_path, repo)
    assert_overview_status(artifacts, uploaded_v2(uploads), "deterministic")


def test_generate_overview_raising_keeps_the_detailed_document_and_the_upload(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("overview bug")

    monkeypatch.setattr(onboarding, "generate_overview", explode)
    log = run_scan(tmp_path, repo)
    document = uploaded_v2(uploads)
    assert "overview" not in document and document["generated_by"] == "llm"
    assert_valid(document)
    assert status_of(artifacts)["tier"] == "llm"
    assert status_of(artifacts)["overview"] == {"source": "none", "retried": False, "components": 0, "edges": 0,
                                                "steps": 0, "steps_dropped": 0}
    assert "overview dropped (RuntimeError: overview bug)" in log
    assert V1 in uploads[0].namelist()


def test_generate_overview_returning_nothing_keeps_the_detailed_document(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(onboarding, "generate_overview",
                        lambda *a, **k: {"overview": None, "warnings": [], "stats": {"source": "none", "retried": True}})
    run_scan(tmp_path, repo)
    assert "overview" not in uploaded_v2(uploads)
    assert status_of(artifacts)["overview"]["source"] == "none" and status_of(artifacts)["overview"]["retried"] is True


def test_a_failing_final_validation_with_the_overview_drops_only_the_overview(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    attempts = []

    def reject_overview(document, head, snapshot):
        attempts.append("overview" in document)
        if "overview" in document:
            raise ValueError("overview rejected late")
        return validate_v2(document, head, snapshot)

    monkeypatch.setattr(onboarding, "validate_v2", reject_overview)
    log = run_scan(tmp_path, repo)
    document = uploaded_v2(uploads)
    assert "overview" not in document and document["generated_by"] == "llm" and document["steps"]
    assert True in attempts
    assert status_of(artifacts)["tier"] == "llm" and status_of(artifacts)["overview"]["source"] == "none"
    assert "overview dropped (ValueError: overview rejected late)" in log


def test_an_overview_that_really_fails_strict_validation_is_never_uploaded(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    real = onboarding.generate_overview

    def tampered(*args, **kwargs):
        result = real(*args, **kwargs)
        result["overview"]["components"][0]["stats"]["files"] += 5
        return result

    monkeypatch.setattr(onboarding, "generate_overview", tampered)
    log = run_scan(tmp_path, repo)
    document = uploaded_v2(uploads)
    assert "overview" not in document and "stats" in log
    assert_valid(document)


def test_the_environment_switch_disables_only_the_overview(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    monkeypatch.setenv("PAIRCLI_ARCH_OVERVIEW", "0")
    log = run_scan(tmp_path, repo)
    document = uploaded_v2(uploads)
    assert "overview" not in document and document["generated_by"] == "llm"
    assert agent.overview_calls == []
    assert status_of(artifacts)["overview"]["source"] == "none"
    assert "overview disabled by PAIRCLI_ARCH_OVERVIEW=0" in log
    assert not list((artifacts / "diagnostics" / "architecture-llm").glob("overview-*"))


def test_a_deterministic_detailed_document_gets_the_deterministic_overview_without_a_model_call(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path, agent=Agent(group="garbage", merge="garbage"))
    run_scan(tmp_path, repo)
    document = uploaded_v2(uploads)
    assert document["generated_by"] == "deterministic"
    assert agent.overview_calls == []
    assert document["overview"]["generated_by"] == "deterministic"
    assert_valid(document)
    assert_overview_status(artifacts, document, "deterministic")


def test_an_unavailable_harness_still_gets_the_deterministic_overview(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    run_scan(tmp_path, repo, harness="other")
    document = uploaded_v2(uploads)
    assert agent.overview_calls == [] and document["overview"]["generated_by"] == "deterministic"
    assert_valid(document)
    assert_overview_status(artifacts, document, "deterministic")


def test_the_error_fallback_document_also_gets_the_deterministic_overview(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("generate bug")

    monkeypatch.setattr(onboarding, "generate_v2", explode)
    run_scan(tmp_path, repo)
    document = uploaded_v2(uploads)
    assert document["generated_by"] == "deterministic" and document["overview"]["generated_by"] == "deterministic"
    assert status_of(artifacts)["reason"] == "v2 error: RuntimeError"
    assert_overview_status(artifacts, document, "deterministic")


def test_a_broken_deterministic_overview_does_not_stop_the_deterministic_document(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("overview builder bug")

    monkeypatch.setattr(onboarding, "deterministic_overview", explode)
    run_scan(tmp_path, repo, harness="other")
    document = uploaded_v2(uploads)
    assert "overview" not in document and document["generated_by"] == "deterministic"
    assert status_of(artifacts)["overview"]["source"] == "none"
    assert V1 in uploads[0].namelist()


def test_the_overview_step_never_raises_with_a_broken_log(monkeypatch, tmp_path):
    class BrokenLog:
        def write(self, _message):
            raise OSError("closed")

    document = {"head_sha": HEAD}
    result, status = onboarding._v2_attach_overview(document, code_snapshot(), HEAD, tmp_path, BrokenLog(), generate=None)
    assert result is document and status["source"] == "none"


def test_docs_only_and_skipped_diagrams_have_no_overview(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path, snapshot=docs_snapshot())
    run_scan(tmp_path, repo)
    assert V2 not in uploads[0].namelist() and agent.overview_calls == []
    assert status_of(artifacts)["tier"] == "none" and status_of(artifacts)["overview"]["source"] == "none"


def test_the_zip_root_alias_carries_the_overview(monkeypatch, tmp_path):
    repo, artifacts, uploads, agent = prepare(monkeypatch, tmp_path)
    run_scan(tmp_path, repo)
    nested = json.loads(uploads[0].read(f".paircli/pr-42/{HEAD}/{V2}"))
    alias = json.loads(uploads[0].read(V2))
    assert alias["overview"]["components"] and alias == nested
    assert alias["overview"]["generated_by"] == "llm"
    assert not any("overview-" in name or "diagnostics" in name for name in uploads[0].namelist())
