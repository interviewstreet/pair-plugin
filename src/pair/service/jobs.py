"""Job manager for the review service.

Bounded concurrency (at most two jobs at a time), section-parallel writers
through an injectable :class:`ClaudeRunner`, and a strict write order so a
``done`` manifest at the storage prefix always implies ``bundle.tar.gz``
exists and matches the manifest packed inside it.

The manager reuses ``pair.brief`` for input building, validation, save, and
summary-input refresh; those functions already carry the invariants the CLI
relies on and re-writing them here would drift.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pair.brief import (
    MODEL_SECTIONS,
    SaveError,
    refresh_summary_input,
    save,
    section_inputs,
    write_input,
)
from pair.util import dumps, write_atomic

from pair.service.archive import ArchiveError, extract_safe
from pair.service.claude import (
    ClaudeRunner,
    LoginCheck,
    WRITERS,
    build_prompt,
    load_agent_prompt,
)
from pair.service.manifest import ManifestState
from pair.service.render import (
    AGENTS_MD,
    BUNDLE_INCLUDES,
    SESSIONS_SUBDIR,
    build_bundle,
    render_brief_md,
    render_sessions,
)
from pair.service.storage import (
    Storage,
    bundle_path,
    manifest_path,
    next_payload_path,
    review_object,
)

log = logging.getLogger("pair.service.jobs")


# The three subagent-driven sections that run in parallel before the summary.
PARALLEL_SECTIONS: tuple[str, ...] = ("side_effects", "plan_vs_result", "pushback")


@dataclass
class JobResult:
    """Terminal state exposed to tests. ``error`` is None on success."""

    key: str
    status: str  # "done" or "failed"
    error: str | None = None


@dataclass
class Job:
    key: str
    manager: "JobManager"
    meta: dict
    payload_path: str
    thread: threading.Thread | None = None
    done_evt: threading.Event = field(default_factory=threading.Event)
    result: JobResult | None = None

    def wait(self, timeout: float | None = None) -> JobResult | None:
        self.done_evt.wait(timeout)
        return self.result


# --------------------------------------------------------------------------- #
#  Manager
# --------------------------------------------------------------------------- #


class JobManager:
    """Owns the concurrency semaphore, the runner, and job bookkeeping."""

    def __init__(
        self,
        *,
        storage: Storage,
        runner: ClaudeRunner,
        agents_dir: Path,
        login_check: LoginCheck | Callable[[], bool] | None = None,
        max_concurrent: int = 2,
        signer_ttl_seconds: int = 15 * 60,
    ) -> None:
        self.storage = storage
        self.runner = runner
        self.agents_dir = Path(agents_dir)
        self.login_check = login_check or (lambda: True)
        self.max_concurrent = max_concurrent
        self.signer_ttl_seconds = signer_ttl_seconds
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._active = 0
        self._active_lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._jobs_lock = threading.Lock()

    # -- payload archive --------------------------------------------------- #

    def store_payload(self, key: str, payload: bytes) -> str:
        """Upload the raw submitted archive; used by the HTTP layer before ack."""
        path = next_payload_path(self.storage, key)
        self.storage.put_bytes(path, payload)
        return path

    # -- job lifecycle ----------------------------------------------------- #

    def status_for(self, key: str) -> str | None:
        """Return the current top-level status for ``key``, or None if unknown.

        We ask storage rather than the in-memory job set so we correctly see
        keys from previous process lifetimes.
        """
        path = manifest_path(key)
        if not self.storage.exists(path):
            return None
        try:
            manifest = json.loads(self.storage.get_bytes(path).decode())
        except (ValueError, UnicodeDecodeError):
            return None
        return manifest.get("status")

    def start(self, key: str, meta: dict, payload_ref: str) -> Job:
        job = Job(key=key, manager=self, meta=meta, payload_path=payload_ref)
        with self._jobs_lock:
            self._jobs[key] = job
        job.thread = threading.Thread(
            target=job_runner, args=(job,), name=f"job-{key}", daemon=True
        )
        job.thread.start()
        return job

    def active_count(self) -> int:
        with self._active_lock:
            return self._active

    def get(self, key: str) -> Job | None:
        with self._jobs_lock:
            return self._jobs.get(key)

    def wait_all(self, timeout: float | None = None) -> None:
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        for j in jobs:
            j.done_evt.wait(timeout)

    # -- publish helpers used by Job -------------------------------------- #

    def _publish_manifest(self, manifest: ManifestState, *, status: str | None = None) -> None:
        self.storage.put_bytes(
            manifest_path(manifest.key),
            manifest.to_json(status=status).encode("utf-8"),
        )

    def _publish_file(self, key: str, work: Path, name: str) -> None:
        p = work / name
        if not p.exists():
            return
        self.storage.put_bytes(review_object(key, name), p.read_bytes())

    # -- called by Job when it acquires/releases the concurrency slot ----- #

    def _enter_active(self) -> None:
        with self._active_lock:
            self._active += 1

    def _exit_active(self) -> None:
        with self._active_lock:
            self._active -= 1


# --------------------------------------------------------------------------- #
#  Job pipeline
# --------------------------------------------------------------------------- #


def job_runner(job: Job) -> None:
    """Top-level thread target: acquires the semaphore, runs, records result."""
    m = job.manager
    with m._sem:
        m._enter_active()
        try:
            _run_pipeline(job)
        except Exception as exc:
            log.exception("job %s: uncaught error", job.key)
            # Any uncaught exception mid-pipeline: publish a failed manifest
            # if we can, so /reviews/{key} shows the failure instead of
            # sticking at queued forever.
            try:
                manifest = _initial_manifest(job)
                for name in manifest.sections:
                    manifest.mark(name, "failed", error=str(exc))
                m._publish_manifest(manifest, status="failed")
            except Exception:  # pragma: no cover - meta may be malformed
                pass
            job.result = JobResult(key=job.key, status="failed", error=str(exc))
        finally:
            m._exit_active()
            job.done_evt.set()


def _fail(job: Job, manifest: ManifestState | None, message: str) -> None:
    """Record failure, mark whatever manifest we have as failed, no bundle."""
    log.warning("job %s failed: %s", job.key, message)
    if manifest is not None:
        # Mark any section that never ran as failed too, so top status is
        # ``failed`` and the reason is captured somewhere.
        for name, state in manifest.sections.items():
            if state.status in ("queued", "running") and state.error is None:
                manifest.mark(name, "failed", error=message)
        job.manager._publish_manifest(manifest, status="failed")
    job.result = JobResult(key=job.key, status="failed", error=message)


def _run_writer(
    *,
    work: Path,
    section: str,
    runner: ClaudeRunner,
    agents_dir: Path,
) -> None:
    """Run one writer subagent, save its reply, and retry once on validation error.

    Raises :class:`SaveError` on final failure and :class:`Exception` for
    runner or IO problems; both are recorded as the section's ``error``.
    """
    agent_body = load_agent_prompt(agents_dir, section)
    input_text = (work / "inputs" / f"{section}.md").read_text()
    prompt = build_prompt(agent_body, input_text)
    raw = runner.run(section, prompt)
    try:
        save(work, section, raw)
        return
    except SaveError as exc:
        first_error = str(exc)
    retry_prompt = build_prompt(agent_body, input_text, retry_hint=first_error)
    raw = runner.run(section, retry_prompt)
    save(work, section, raw)  # a second SaveError propagates


def _run_summary(
    *,
    work: Path,
    runner: ClaudeRunner,
    agents_dir: Path,
) -> None:
    """The summary writer produces two sections in one call.

    The reply is expected to parse as ``{"summary": [...], "look_first": [...]}``.
    On a SaveError from either section, we retry the whole summary call once
    with the error as a hint, then let the SaveError propagate.
    """
    agent_body = load_agent_prompt(agents_dir, "summary")
    input_text = (work / "inputs" / "summary.md").read_text()

    def _parse(raw: str) -> tuple[str, str]:
        payload = json.loads(_strip_fences(raw))
        if not isinstance(payload, dict) or "summary" not in payload or "look_first" not in payload:
            raise SaveError(
                'summary writer must reply with {"summary": [...], "look_first": [...]}'
            )
        return json.dumps(payload["summary"]), json.dumps(payload["look_first"])

    def _attempt(prompt: str) -> None:
        raw = runner.run("summary", prompt)
        summary_raw, look_first_raw = _parse(raw)
        save(work, "summary", summary_raw)
        save(work, "look_first", look_first_raw)

    try:
        _attempt(build_prompt(agent_body, input_text, section="summary"))
        return
    except SaveError as exc:
        first_error = str(exc)
    _attempt(
        build_prompt(agent_body, input_text, retry_hint=first_error, section="summary")
    )


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        # Same one-off cleanup pair.brief._parse_items does.
        import re

        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    return text


def _collect_jsonl_paths(work: Path) -> list[str]:
    """Every JSONL file the payload put anywhere in the work tree.

    The plugin namespaces raw session files under ``sessions/<agent>/`` with
    the store-relative path below that, so this has to recurse rather than
    scan one directory.
    """
    return sorted(str(p) for p in work.rglob("*.jsonl"))


def _session_roles(brief: dict) -> dict[str, str]:
    """``<agent>:<id>`` to match role, from the session list in ``brief.json``."""
    roles = {}
    for s in brief.get("sessions") or []:
        if isinstance(s, dict) and s.get("agent") and s.get("id") and s.get("role"):
            roles[f"{s['agent']}:{s['id']}"] = str(s["role"])
    return roles


def _reset_model_sections(work: Path, *, writer_sections: list[str]) -> None:
    """Empty the model sections in ``brief.json`` and re-arm ``pending``.

    The submitted brief carries the plugin's placeholders for the sections a
    writer has yet to fill. Only this service fills them, so anything already
    there is stale by construction, and leaving it would let a placeholder
    survive into a ``done`` bundle. ``pending`` is rebuilt from what we are
    actually about to run, so ``save`` drains it to empty on success.
    """
    path = work / "brief.json"
    brief = json.loads(path.read_text())
    sections = brief.setdefault("sections", {})
    for name in MODEL_SECTIONS:
        sections[name] = []
    brief["pending"] = [
        s for s in MODEL_SECTIONS
        if s in writer_sections or s in ("summary", "look_first")
    ]
    write_atomic(path, dumps(brief))


def _publish_bundle_files(job: Job, work: Path, *, include_sessions: bool = True) -> None:
    """Push every bundle file that already exists to storage."""
    m = job.manager
    for name in BUNDLE_INCLUDES:
        m._publish_file(job.key, work, name)
    sess_dir = work / SESSIONS_SUBDIR
    if include_sessions and sess_dir.is_dir():
        for entry in sorted(sess_dir.iterdir()):
            if not entry.is_file() or entry.suffix == ".jsonl":
                continue
            m.storage.put_bytes(
                review_object(job.key, f"{SESSIONS_SUBDIR}/{entry.name}"),
                entry.read_bytes(),
            )


def _run_pipeline(job: Job) -> None:
    m = job.manager
    with tempfile.TemporaryDirectory(prefix=f"pair-{job.key}-") as tmp:
        work = Path(tmp)

        # 1. Extract the submitted archive. On error, no manifest exists yet;
        #    still record failure by writing a failed manifest built from meta.
        try:
            payload_bytes = m.storage.get_bytes(job.payload_path)
            extract_safe(payload_bytes, work)
        except (ArchiveError, FileNotFoundError, KeyError) as exc:
            manifest = _initial_manifest(job)
            _fail(job, manifest, f"payload rejected: {exc}")
            return

        # 2. Initial "queued" manifest — all five sections queued, same stamp.
        manifest = _initial_manifest(job)
        m._publish_manifest(manifest, status="queued")

        # 3. The payload's brief and evidence drive everything below, so read
        #    them before any rendering and fail fast if they're missing.
        try:
            evidence = json.loads((work / "evidence.json").read_text())
            brief = json.loads((work / "brief.json").read_text())
        except (OSError, ValueError) as exc:
            _fail(job, manifest, f"payload missing brief/evidence: {exc}")
            return

        # 4. Render sessions from raw JSONL. This mints the .md the bundle
        #    ships and drops the raw JSONL from the shareable copy. The
        #    plugin's match roles are the only place a role is known, so they
        #    come across in brief.json and get copied into the index.
        sessions_dir = work / SESSIONS_SUBDIR
        jsonl_paths = _collect_jsonl_paths(work)
        try:
            index = render_sessions(jsonl_paths, sessions_dir, roles=_session_roles(brief))
        except Exception as exc:  # pragma: no cover - render is best-effort
            _fail(job, manifest, f"session render failed: {exc}")
            return
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / "index.json").write_text(json.dumps(index, indent=2))
        # AGENTS.md is a static docblock; write once, from render.py.
        (work / "AGENTS.md").write_text(AGENTS_MD)

        # 5. Rebuild the writer-input markdown from evidence.json. The plugin
        #    normally leaves ``inputs/*.md`` in the payload; the service
        #    regenerates them so an old or edited payload can't smuggle a
        #    stale prompt into a writer.
        inputs = section_inputs(evidence, brief["sections"])
        writer_sections = [s for s in PARALLEL_SECTIONS if s in inputs]
        # The payload carries whatever the plugin had in the model sections,
        # which is a placeholder at best: the writers below are the only thing
        # that fills them, so clear them before any input is rendered.
        _reset_model_sections(work, writer_sections=writer_sections)
        for name, data in inputs.items():
            write_input(work, name, data)
        refresh_summary_input(work)

        # 6. Publish the "first wave": everything we can already share, so
        #    the reader can start rendering the header before writers reply.
        _publish_bundle_files(job, work, include_sessions=True)

        # 7. Mark parallel writer sections as running (only those with inputs).
        skipped = [s for s in PARALLEL_SECTIONS if s not in inputs]
        # Sections with no evidence to write are already correct in the brief
        # (empty list), so we mark them done at the manifest level too.
        for name in skipped:
            manifest.mark(name, "done")
        for name in writer_sections:
            manifest.mark(name, "running")
        m._publish_manifest(manifest)

        # 8. Run the three writers in parallel; validate-retry-fail per section.
        section_errors: list[str] = []
        if writer_sections:
            with ThreadPoolExecutor(
                max_workers=len(writer_sections), thread_name_prefix=f"w-{job.key}"
            ) as ex:
                futures = {
                    ex.submit(
                        _run_writer,
                        work=work,
                        section=name,
                        runner=m.runner,
                        agents_dir=m.agents_dir,
                    ): name
                    for name in writer_sections
                }
                for fut in futures:
                    name = futures[fut]
                    try:
                        fut.result()
                        manifest.mark(name, "done")
                    except SaveError as exc:
                        manifest.mark(name, "failed", error=str(exc))
                        section_errors.append(f"{name}: {exc}")
                    except Exception as exc:  # runner error, timeout, etc.
                        manifest.mark(name, "failed", error=str(exc))
                        section_errors.append(f"{name}: {exc}")
                    m._publish_manifest(manifest)
                    m._publish_file(job.key, work, "brief.json")

        if section_errors:
            # A failure in the first wave leaves summary/look_first queued and
            # tops the manifest as ``failed``. No bundle.tar.gz is written.
            _fail(job, manifest, "; ".join(section_errors))
            return

        # 9. Summary + look_first as one Claude call.
        refresh_summary_input(work)
        manifest.mark("summary", "running")
        manifest.mark("look_first", "running")
        m._publish_manifest(manifest)
        try:
            _run_summary(work=work, runner=m.runner, agents_dir=m.agents_dir)
        except SaveError as exc:
            manifest.mark("summary", "failed", error=str(exc))
            manifest.mark("look_first", "failed", error=str(exc))
            _fail(job, manifest, f"summary: {exc}")
            return
        except Exception as exc:
            manifest.mark("summary", "failed", error=str(exc))
            manifest.mark("look_first", "failed", error=str(exc))
            _fail(job, manifest, f"summary runner: {exc}")
            return
        manifest.mark("summary", "done")
        manifest.mark("look_first", "done")
        m._publish_manifest(manifest)
        m._publish_file(job.key, work, "brief.json")

        # 10. Render human-readable brief.md from the completed brief.
        brief_final = json.loads((work / "brief.json").read_text())
        (work / "brief.md").write_text(render_brief_md(brief_final))
        m._publish_file(job.key, work, "brief.md")

        # 11. The manifest packed inside bundle.tar.gz is the *done* one. We
        #     write it into the work dir, tar everything, upload the archive,
        #     and only then flip the prefix manifest to done.
        done_json = manifest.to_json(status="done")
        (work / "manifest.json").write_text(done_json)
        # Sessions/index.json already written; final AGENTS.md already written.
        bundle_bytes = build_bundle(work)
        m.storage.put_bytes(bundle_path(job.key), bundle_bytes)
        m.storage.put_bytes(manifest_path(job.key), done_json.encode("utf-8"))
        job.result = JobResult(key=job.key, status="done", error=None)


def _initial_manifest(job: Job) -> ManifestState:
    meta = job.meta
    return ManifestState.initial(
        key=job.key,
        repo=meta["repo"],
        pr=int(meta["pr"]),
        title=meta["title"],
        base_sha=meta["base_sha"],
        head_sha=meta["head_sha"],
    )
