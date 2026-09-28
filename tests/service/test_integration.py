"""The plugin/service boundary, end to end, with nothing faked but the model.

Every other test in this directory hand-builds a payload. This one runs the
real ``pair.brief.prepare`` over the session fixtures, packs it with the real
``pair.submit.build_payload``, and POSTs those bytes to the real FastAPI app.
Storage is :class:`InMemoryStorage`, the writer is a fake that answers from
its own input, and the login check is a lambda, so no Claude process starts
and no GCS credential is needed.

What it pins:

* a real payload is accepted (``202``) and reaches a ``done`` manifest and a
  ``bundle.tar.gz``;
* the plugin's ``sessions/<agent>/<store-relative path>`` layout is what
  ``jobs.py`` discovers, including the Claude subagent file;
* raw JSONL is rendered into ``sessions/index.json`` plus markdown and is not
  in the bundle;
* the placeholders the plugin leaves in the pending model sections are
  replaced by writer output;
* neither the submit path nor the service spawns a subprocess, so the old
  local review server never starts and Claude is never called on the builder.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pair.brief as brief_mod
from pair.brief import MODEL_SECTIONS, prepare
from pair.config import load_config
from pair.service.app import ServiceConfig, create_app
from pair.service.jobs import JobManager
from pair.service.manifest import SECTION_NAMES
from pair.service.storage import InMemoryStorage, bundle_path, manifest_path
from pair.submit import build_payload
from pair.util import submit_key

from .helpers import TEST_TOKEN, auth_headers, run_and_wait

# The four shipped writer prompts, not the stubs under tests/fixtures: a real
# payload should survive the prompts the image actually carries.
AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"

REF_RX = re.compile(
    r"(?:claude|codex):[A-Za-z0-9._-]+:[A-Za-z0-9._-]+|hunk:\S+?:\d+|file:\S+|pr:body"
)

WRITTEN = {
    "side_effects": "FAKE side effect: this line came from the writer, not the plugin.",
    "plan_vs_result": "FAKE departure: this line came from the writer, not the plugin.",
    "pushback": "FAKE pushback: this line came from the writer, not the plugin.",
    "summary": "FAKE summary: this line came from the writer, not the plugin.",
    "look_first": "FAKE look_first: this line came from the writer, not the plugin.",
}


def allowed_refs(prompt: str) -> list[str]:
    """The refs from the input's ``allowed_refs`` block, in order.

    Scanning the whole prompt would also pick up the example refs in the
    writer prompts, which are not in this PR's evidence and would be dropped
    by ``validate``. ``allowed_refs`` is rendered last, so everything after
    the final occurrence of that key belongs to the input.
    """
    _, _, tail = prompt.rpartition("allowed_refs:")
    return list(dict.fromkeys(REF_RX.findall(tail)))


@dataclass
class EchoRunner:
    """Answers each writer with one item citing a ref lifted from its own input.

    This never touches Claude. Echoing a ref back proves the service built a
    real input for the section and handed it to the writer; the canned text
    proves the reply is what lands in ``brief.json``.
    """

    calls: list[str] = field(default_factory=list)

    def run(self, section: str, prompt: str) -> str:
        self.calls.append(section)
        refs = allowed_refs(prompt)
        assert refs, f"{section} was given an input with no allowed_refs"
        hunks = [r for r in refs if r.startswith("hunk:")]
        if section == "summary":
            return json.dumps({
                "summary": [{"text": WRITTEN["summary"], "refs": refs[:1]}],
                "look_first": [{"text": WRITTEN["look_first"], "refs": hunks[:1]}],
            })
        return json.dumps([{"text": WRITTEN[section], "refs": refs[:1]}])


@pytest.fixture
def plugin_payload(demo, pr7, monkeypatch):
    """A real submit payload: prepare() over the fixtures, packed by submit.py."""
    monkeypatch.setattr(brief_mod, "fetch_pr", lambda repo, n, cfg: pr7)
    prepared = prepare("acme/demo#7", load_config())
    body, meta = build_payload(prepared)
    return {"body": body, "meta": meta, "prepared": prepared}


@pytest.fixture
def service(monkeypatch):
    """App + manager over in-memory storage, a fake writer, and a fake login."""
    storage = InMemoryStorage()
    runner = EchoRunner()
    manager = JobManager(
        storage=storage,
        runner=runner,
        agents_dir=AGENTS_DIR,
        login_check=lambda: True,
        max_concurrent=2,
    )
    app = create_app(
        manager=manager,
        config=ServiceConfig(bearer_token=TEST_TOKEN),
        login_check=manager.login_check,
        access_token=lambda: "ya29.test-token",
    )
    with TestClient(app) as client:
        yield {"client": client, "manager": manager, "storage": storage, "runner": runner}


def _post(service, body: bytes):
    return service["client"].post(
        "/submit",
        content=body,
        params={"force": "false"},
        headers={**auth_headers(), "Content-Type": "application/gzip"},
    )


def _payload_member(body: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        return tar.extractfile(name).read()


def _bundle_members(storage: InMemoryStorage, key: str) -> dict[str, bytes]:
    raw = storage.get_bytes(bundle_path(key))
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        return {
            m.name: tar.extractfile(m).read()
            for m in tar.getmembers()
            if m.isreg()
        }


@pytest.fixture
def finished(plugin_payload, service):
    """Submit the real payload and wait for the job to finish."""
    response = _post(service, plugin_payload["body"])
    assert response.status_code == 202, response.text
    key = plugin_payload["meta"]["key"]
    assert response.json() == {"key": key}
    result = run_and_wait(service["manager"], key, timeout=30.0)
    assert result.status == "done", result.error
    return {**service, **plugin_payload, "key": key}


# --------------------------------------------------------------------------- #
#  1. A real payload reaches the service and produces a bundle
# --------------------------------------------------------------------------- #


def test_meta_json_is_exactly_the_six_contract_fields(plugin_payload):
    with tarfile.open(fileobj=io.BytesIO(plugin_payload["body"]), mode="r:gz") as tar:
        meta = json.loads(tar.extractfile("meta.json").read().decode())
    assert list(meta) == ["key", "repo", "pr", "title", "base_sha", "head_sha"]
    # base_sha is the merge base of head and the PR's base branch, which for
    # the pr7 fixture is a third SHA, distinct from both.
    basics = plugin_payload["prepared"]["pr_basics"]
    assert meta["base_sha"] == basics["merge_base"]
    assert meta["base_sha"] not in (basics["head_sha"], "")
    assert meta["key"] == submit_key(meta["repo"], meta["pr"], meta["head_sha"])


def test_real_payload_is_accepted_and_produces_a_done_manifest_and_bundle(finished):
    manifest = json.loads(finished["storage"].get_bytes(manifest_path(finished["key"])).decode())
    assert manifest["status"] == "done"
    assert manifest["key"] == finished["key"]
    assert manifest["base_sha"] == finished["meta"]["base_sha"]
    assert manifest["head_sha"] == finished["meta"]["head_sha"]
    assert bundle_path(finished["key"]) in finished["storage"].blobs

    members = _bundle_members(finished["storage"], finished["key"])
    for name in ("manifest.json", "brief.json", "brief.md", "evidence.json",
                 "diff.patch", "risky_hunks.json", "AGENTS.md", "sessions/index.json"):
        assert name in members, sorted(members)
    # The manifest inside the bundle is the done one, and it matches the prefix.
    assert json.loads(members["manifest.json"].decode()) == manifest

    review = finished["client"].get(f"/reviews/{finished['key']}", headers=auth_headers())
    assert review.status_code == 200
    assert review.json()["bundle_url"].startswith("https://storage.googleapis.com/")
    assert review.json()["bundle_token"] == "ya29.test-token"


# --------------------------------------------------------------------------- #
#  2. Pending placeholders are replaced and every section runs
# --------------------------------------------------------------------------- #


def test_every_section_becomes_done_and_writer_output_replaces_the_placeholder(finished):
    manifest = json.loads(finished["storage"].get_bytes(manifest_path(finished["key"])).decode())
    for name in SECTION_NAMES:
        assert manifest["sections"][name]["status"] == "done", name
        assert manifest["sections"][name]["error"] is None, name

    submitted = json.loads(_payload_member(finished["body"], "brief.json").decode())
    assert submitted["pending"] == MODEL_SECTIONS, "the plugin should ship every model section pending"

    brief = json.loads(_bundle_members(finished["storage"], finished["key"])["brief.json"].decode())
    assert brief["pending"] == [], brief["pending"]
    for name in MODEL_SECTIONS:
        items = brief["sections"][name]
        assert items, f"{name} is still empty after the writers ran"
        assert [it["text"] for it in items] == [WRITTEN[name]], name
        assert all(it["refs"] for it in items), name

    # And every writer really was called: three parallel sections plus summary.
    assert sorted(finished["runner"].calls) == [
        "plan_vs_result", "pushback", "side_effects", "summary",
    ]


def test_brief_md_carries_the_writer_text_not_the_placeholder(finished):
    brief_md = _bundle_members(finished["storage"], finished["key"])["brief.md"].decode()
    for name in MODEL_SECTIONS:
        assert f"## {name}" in brief_md, name
        assert WRITTEN[name] in brief_md, name


# --------------------------------------------------------------------------- #
#  3. The plugin's raw-JSONL layout is what session discovery expects
# --------------------------------------------------------------------------- #


def test_payload_namespaces_raw_jsonl_by_agent(plugin_payload):
    with tarfile.open(fileobj=io.BytesIO(plugin_payload["body"]), mode="r:gz") as tar:
        names = tar.getnames()
    jsonl = [n for n in names if n.endswith(".jsonl")]
    assert any(n.startswith("sessions/claude/") for n in jsonl), jsonl
    assert any(n.startswith("sessions/codex/") for n in jsonl), jsonl
    # The Claude subagent file keeps its store-relative sub-path, which is what
    # readers/claude.py checks for to flag a session as a subagent run.
    assert any("/subagents/" in n for n in jsonl), jsonl


def test_sessions_are_rendered_to_index_and_markdown_with_no_raw_jsonl(finished):
    members = _bundle_members(finished["storage"], finished["key"])
    assert not [n for n in members if n.endswith(".jsonl")], sorted(members)

    index = json.loads(members["sessions/index.json"].decode())
    assert index, "at least one session should be rendered"
    agents = {entry["agent"] for entry in index}
    assert {"claude", "codex"} <= agents, agents
    for entry in index:
        # The contract's minimum for an index entry.
        for field_name in ("agent", "id", "file", "role"):
            assert entry.get(field_name), (field_name, entry)
        assert entry["file"] == f"sessions/{entry['agent']}-{entry['id']}.md"
        assert entry["file"] in members, (entry["file"], sorted(members))
        assert entry["turns"] > 0, entry

    # One entry per session id: the subagent file folds into its parent rather
    # than overwriting the parent's markdown.
    ids = [entry["id"] for entry in index]
    assert len(ids) == len(set(ids)), ids

    # Refs in the brief resolve inside the bundle by plain text search, which
    # is the whole point of rendering the transcripts.
    brief = json.loads(members["brief.json"].decode())
    session_text = "\n".join(
        members[entry["file"]].decode() for entry in index
    )
    agent_refs = [
        ref
        for items in brief["sections"].values()
        for item in items
        for ref in item["refs"]
        if ref.startswith(("claude:", "codex:"))
    ]
    assert agent_refs, "expected at least one session-backed ref in the brief"
    for ref in agent_refs:
        assert ref in session_text, ref


def test_subagent_turns_land_in_the_parent_transcript(finished):
    members = _bundle_members(finished["storage"], finished["key"])
    index = json.loads(members["sessions/index.json"].decode())
    by_id = {entry["id"]: entry for entry in index}
    # sess-main-1 is the fixture session with a subagents/ file beside it.
    assert "sess-main-1" in by_id, sorted(by_id)
    entry = by_id["sess-main-1"]
    assert len(entry["sources"]) > 1, entry
    md = members[entry["file"]].decode()
    assert "subagent: true" in md, "the subagent's turns are missing"
    assert "Write the session helper" in md, "the subagent's prompt is missing"
    assert "## turn claude:sess-main-1:" in md


# --------------------------------------------------------------------------- #
#  4. Nothing shells out: no local server, no Claude on the builder
# --------------------------------------------------------------------------- #


def test_submit_never_shells_out_to_a_server_or_to_claude(plugin_payload, service, monkeypatch):
    """`POST /submit` and the job behind it must not spawn any process.

    The old flow started ``pair.server`` from the builder and ran Claude there.
    Both are subprocess spawns, so banning ``subprocess`` for the duration of
    the request covers them together, and ``ensure_server`` is stubbed
    separately in case it ever stops shelling out.
    """
    import pair.server

    def boom(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess: {args!r}")

    monkeypatch.setattr(pair.server, "ensure_server", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)

    response = _post(service, plugin_payload["body"])
    assert response.status_code == 202
    result = run_and_wait(service["manager"], plugin_payload["meta"]["key"], timeout=30.0)
    assert result.status == "done", result.error


def test_submit_response_carries_no_server_url(plugin_payload, service):
    """The 202 body is the key and nothing else — no local review URL."""
    response = _post(service, plugin_payload["body"])
    assert response.json() == {"key": plugin_payload["meta"]["key"]}
    run_and_wait(service["manager"], plugin_payload["meta"]["key"], timeout=30.0)
