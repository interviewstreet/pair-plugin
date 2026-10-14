"""Capture the new raw transcript range for one PR head commit."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_CAPTURE_BYTES = 80 * 1024 * 1024


@dataclass(frozen=True)
class PendingTranscriptCapture:
    """A staged capture whose cursor can be committed after upload succeeds."""

    state_path: Path
    state: dict[str, Any]
    manifest_path: Path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"could not read Pair snapshot {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Pair snapshot {path} is not a JSON object")
    return value


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip(".-")
    return cleaned[:120] or hashlib.sha256(value.encode()).hexdigest()[:16]


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _line_timestamp(entry: dict[str, Any]) -> datetime | None:
    return _timestamp(entry.get("timestamp"))


def _is_claude_prompt(entry: dict[str, Any]) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta") is True or entry.get("isCompactSummary") is True:
        return False
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not content.lstrip().startswith("<")
    if not isinstance(content, list) or any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    ):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).strip()
        for block in content
    )


def _is_codex_prompt(entry: dict[str, Any]) -> bool:
    if entry.get("type") != "event_msg":
        return False
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return False
    if payload.get("type") == "user_message":
        return bool(str(payload.get("message", "")).strip())
    if payload.get("type") != "item_completed":
        return False
    item = payload.get("item")
    return isinstance(item, dict) and item.get("type") == "UserMessage"


def _first_prompt_offset(raw: bytes, harness: str, after: datetime | None) -> int | None:
    offset = 0
    prompt_test = _is_codex_prompt if harness == "codex" else _is_claude_prompt
    for line in raw.splitlines(keepends=True):
        try:
            entry = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            offset += len(line)
            continue
        if isinstance(entry, dict) and prompt_test(entry):
            timestamp = _line_timestamp(entry)
            if after is None or timestamp is None or timestamp > after:
                return offset
        offset += len(line)
    return None


def _sha256_prefix(path: Path, length: int) -> str:
    digest = hashlib.sha256()
    remaining = length
    with path.open("rb") as source:
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    if remaining:
        return ""
    return digest.hexdigest()


def _cursor_offset(path: Path, cursor: dict[str, Any] | None) -> int:
    if not cursor:
        return 0
    offset = cursor.get("offset")
    digest = cursor.get("prefix_sha256")
    if not isinstance(offset, int) or offset < 0 or not isinstance(digest, str):
        return 0
    try:
        if path.stat().st_size < offset or _sha256_prefix(path, offset) != digest:
            return 0
        if offset:
            with path.open("rb") as source:
                source.seek(offset - 1)
                if source.read(1) != b"\n":
                    return 0
    except OSError:
        return 0
    return offset


def _claude_session_file(path: Path, session_id: str) -> bool:
    try:
        with path.open("rb") as source:
            for line in source:
                try:
                    entry = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str):
                    return entry["sessionId"] == session_id
    except OSError:
        return False
    return False


def _claude_sources(main: Path, session_id: str) -> list[tuple[Path, bool]]:
    sources: list[tuple[Path, bool]] = [(main, True)]
    if main.parent.is_dir() and not main.parent.is_symlink():
        sources.extend(
            (path, True) for path in sorted(main.parent.glob("*.jsonl"))
            if path != main and not path.is_symlink() and _claude_session_file(path, session_id)
        )
    subagents = main.parent / session_id / "subagents"
    if subagents.is_dir() and not subagents.is_symlink():
        sources.extend((path, False) for path in sorted(subagents.glob("agent-*.jsonl")))
    return sources


def _sources(session: dict[str, Any]) -> list[tuple[Path, bool]]:
    source_path = session.get("source_path")
    harness = session.get("harness")
    session_id = session.get("id")
    if not all(isinstance(value, str) and value for value in (source_path, harness, session_id)):
        return []
    if harness not in {"codex", "claude-code"}:
        return []
    main = Path(source_path).expanduser()
    return _claude_sources(main, session_id) if harness == "claude-code" else [(main, True)]


def _head_session_refs(snapshot: dict[str, Any], head_sha: str) -> set[str] | None:
    links = snapshot.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and str(link.get("sha", "")).lower() == head_sha:
            sessions = link.get("sessions")
            return {value for value in sessions if isinstance(value, str)} if isinstance(sessions, list) else set()
    return None


def _previous_commit_time(snapshot: dict[str, Any]) -> datetime | None:
    pr = snapshot.get("pr")
    commits = pr.get("commits") if isinstance(pr, dict) else None
    if not isinstance(commits, list) or len(commits) < 2:
        return None
    previous = commits[-2]
    return _timestamp(previous.get("time")) if isinstance(previous, dict) else None


def _load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "sources": {}}
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_VERSION, "sources": {}}
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        return {"schema_version": SCHEMA_VERSION, "sources": {}}
    if not isinstance(state.get("sources"), dict):
        state["sources"] = {}
    return state


def stage_incremental_transcripts(artifact_dir: Path) -> PendingTranscriptCapture:
    """Write new raw JSONL ranges under artifact_dir without advancing cursors."""
    head_sha = artifact_dir.name.lower()
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("incremental transcript capture requires a commit-scoped artifact directory")
    snapshot = _read_json(artifact_dir / "snapshot.json")
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list):
        raise RuntimeError("Pair snapshot is missing linked sessions")

    state_path = artifact_dir.parent / "transcript-state.json"
    prior = _load_state(state_path)
    prior_sources = prior["sources"]
    next_sources = dict(prior_sources)
    previous_time = _previous_commit_time(snapshot)
    head_refs = _head_session_refs(snapshot, head_sha)
    raw_root = artifact_dir / "raw-sessions"
    if raw_root.exists():
        if raw_root.is_symlink():
            raise ValueError("raw transcript directory must not be a symlink")
        shutil.rmtree(raw_root)
    raw_root.mkdir(parents=True)

    manifest_sessions = []
    total = 0
    for session in sessions:
        if not isinstance(session, dict):
            continue
        harness, session_id = session.get("harness"), session.get("id")
        ref = f"{harness}:{session_id}"
        if head_refs is not None and ref not in head_refs:
            continue
        files = []
        main_started = False
        for source, is_main in _sources(session):
            if source.is_symlink() or not source.is_file():
                continue
            source = source.resolve(strict=True)
            key = hashlib.sha256(str(source).encode()).hexdigest()
            offset = _cursor_offset(source, prior_sources.get(key))
            with source.open("rb") as input_file:
                input_file.seek(offset)
                delta = input_file.read(MAX_CAPTURE_BYTES + 1)
            if len(delta) > MAX_CAPTURE_BYTES:
                raise ValueError("raw transcript delta exceeds the 80 MiB capture limit")
            current_size = source.stat().st_size
            next_sources[key] = {
                "path": str(source),
                "offset": current_size,
                "prefix_sha256": _sha256_prefix(source, current_size),
                "session": ref,
            }
            if is_main:
                prompt_offset = _first_prompt_offset(delta, str(harness), previous_time)
                if prompt_offset is None:
                    continue
                delta = delta[prompt_offset:]
                offset += prompt_offset
                main_started = True
            elif not main_started and not files:
                continue
            if not delta:
                continue
            total += len(delta)
            if total > MAX_CAPTURE_BYTES:
                raise ValueError("combined raw transcript delta exceeds the 80 MiB capture limit")
            source_name = f"{key[:12]}-{_safe_component(source.name)}"
            relative = Path(str(harness)) / _safe_component(str(session_id)) / source_name
            destination = raw_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(delta)
            files.append({
                "path": relative.as_posix(),
                "source_name": source.name,
                "from_offset": offset,
                "to_offset": current_size,
                "sha256": hashlib.sha256(delta).hexdigest(),
                "bytes": len(delta),
            })
        if files:
            manifest_sessions.append({"harness": harness, "session_id": session_id, "files": files})

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "head_sha": head_sha,
        "previous_head_sha": prior.get("last_head_sha", ""),
        "sessions": manifest_sessions,
    }
    manifest_path = raw_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    state = {
        "schema_version": SCHEMA_VERSION,
        "last_head_sha": head_sha,
        "sources": next_sources,
    }
    return PendingTranscriptCapture(state_path=state_path, state=state, manifest_path=manifest_path)


def commit_transcript_capture(capture: PendingTranscriptCapture) -> None:
    """Atomically advance transcript cursors after archive upload succeeds."""
    capture.state_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="transcript-state-", dir=capture.state_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(capture.state, output, indent=2)
            output.write("\n")
        temporary.replace(capture.state_path)
    finally:
        temporary.unlink(missing_ok=True)
