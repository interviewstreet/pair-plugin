from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))
SPEC = importlib.util.spec_from_file_location("paircli_transcripts", HOOKS_DIR / "paircli_transcripts.py")
assert SPEC and SPEC.loader
transcripts = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = transcripts
SPEC.loader.exec_module(transcripts)


def write_jsonl(path: Path, entries: list[dict], append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as output:
        for entry in entries:
            output.write(json.dumps(entry) + "\n")


def write_snapshot(artifact_dir: Path, source: Path, harness: str, session_id: str,
                   commits: list[tuple[str, str]]) -> None:
    artifact_dir.mkdir(parents=True)
    head = commits[-1][0]
    snapshot = {
        "pr": {"commits": [{"sha": sha, "time": timestamp} for sha, timestamp in commits]},
        "sessions": [{"harness": harness, "id": session_id, "source_path": str(source)}],
        "links": [{"sha": head, "sessions": [f"{harness}:{session_id}"]}],
    }
    (artifact_dir / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")


def codex_prompt(text: str, timestamp: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [
            {"type": "text", "text": text},
        ]}},
    }


def test_captures_only_new_codex_prompt_range_per_head(tmp_path):
    source = tmp_path / "codex" / "rollout-session-1.jsonl"
    write_jsonl(source, [
        {"timestamp": "2026-10-14T09:59:00Z", "type": "session_meta", "payload": {"id": "session-1"}},
        codex_prompt("build the first change", "2026-10-14T10:00:00Z"),
        {"timestamp": "2026-10-14T10:01:00Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "done"}},
    ])
    pr_dir = tmp_path / "repo" / ".paircli" / "pr-7"
    first = pr_dir / ("a" * 40)
    write_snapshot(first, source, "codex", "session-1", [("a" * 40, "2026-10-14T10:02:00Z")])

    pending = transcripts.stage_incremental_transcripts(first)
    manifest = json.loads(pending.manifest_path.read_text())
    captured = first / "raw-sessions" / manifest["sessions"][0]["files"][0]["path"]
    assert "build the first change" in captured.read_text()
    assert "session_meta" not in captured.read_text()
    assert not pending.state_path.exists()
    transcripts.commit_transcript_capture(pending)

    write_jsonl(source, [
        {"timestamp": "2026-10-14T10:03:00Z", "type": "event_msg", "payload": {"type": "token_count"}},
        codex_prompt("make the follow-up change", "2026-10-14T10:04:00Z"),
        {"timestamp": "2026-10-14T10:05:00Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "updated"}},
    ], append=True)
    second = pr_dir / ("b" * 40)
    write_snapshot(second, source, "codex", "session-1", [
        ("a" * 40, "2026-10-14T10:02:00Z"),
        ("b" * 40, "2026-10-14T10:06:00Z"),
    ])

    next_pending = transcripts.stage_incremental_transcripts(second)
    next_manifest = json.loads(next_pending.manifest_path.read_text())
    next_capture = second / "raw-sessions" / next_manifest["sessions"][0]["files"][0]["path"]
    text = next_capture.read_text()
    assert "make the follow-up change" in text
    assert "build the first change" not in text
    assert "token_count" not in text
    assert next_manifest["previous_head_sha"] == "a" * 40


def test_includes_claude_subagent_jsonl_with_parent_prompt(tmp_path):
    session_id = "claude-session"
    source = tmp_path / "claude" / "project" / f"{session_id}.jsonl"
    write_jsonl(source, [{
        "type": "user", "uuid": "u1", "sessionId": session_id,
        "timestamp": "2026-10-14T10:00:00Z", "promptSource": "typed",
        "message": {"role": "user", "content": "change the API"},
    }])
    resumed = source.parent / "resumed.jsonl"
    write_jsonl(resumed, [{
        "type": "user", "uuid": "u2", "sessionId": session_id,
        "timestamp": "2026-10-14T10:00:30Z", "promptSource": "typed",
        "message": {"role": "user", "content": "also update its test"},
    }])
    subagent = source.parent / session_id / "subagents" / "agent-worker.jsonl"
    write_jsonl(subagent, [{
        "type": "assistant", "uuid": "a1", "sessionId": session_id,
        "timestamp": "2026-10-14T10:01:00Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "editing"}]},
    }])
    artifact_dir = tmp_path / "repo" / ".paircli" / "pr-9" / ("c" * 40)
    write_snapshot(artifact_dir, source, "claude-code", session_id, [
        ("c" * 40, "2026-10-14T10:02:00Z"),
    ])

    pending = transcripts.stage_incremental_transcripts(artifact_dir)
    manifest = json.loads(pending.manifest_path.read_text())

    assert len(manifest["sessions"]) == 1
    assert len(manifest["sessions"][0]["files"]) == 3
    assert {entry["source_name"] for entry in manifest["sessions"][0]["files"]} == {
        f"{session_id}.jsonl", "resumed.jsonl", "agent-worker.jsonl",
    }


def test_unattributed_head_does_not_capture_other_pr_sessions(tmp_path):
    source = tmp_path / "codex" / "rollout-session-1.jsonl"
    write_jsonl(source, [codex_prompt("unrelated earlier work", "2026-10-14T10:00:00Z")])
    artifact_dir = tmp_path / "repo" / ".paircli" / "pr-10" / ("d" * 40)
    write_snapshot(artifact_dir, source, "codex", "session-1", [
        ("d" * 40, "2026-10-14T10:01:00Z"),
    ])
    snapshot_path = artifact_dir / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["links"][0]["sessions"] = []
    snapshot_path.write_text(json.dumps(snapshot))

    pending = transcripts.stage_incremental_transcripts(artifact_dir)
    manifest = json.loads(pending.manifest_path.read_text())

    assert manifest["sessions"] == []
