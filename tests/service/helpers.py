"""Shared test helpers: fake runner + payload builder.

Kept in a plain module rather than ``conftest.py`` so other test files can
``from tests.service.helpers import ...`` without relying on conftest
autoloading. Pytest fixtures live in ``conftest.py`` and reuse the pieces
here.
"""

from __future__ import annotations

import io
import json
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pair.service.jobs import JobManager

FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "fixtures"
BUNDLE_FIXTURES = FIXTURES_ROOT / "bundle"
AGENTS_DIR = FIXTURES_ROOT / "service" / "agents"

TEST_KEY = "interviewstreet__pair-be__62__3453326da379"
TEST_META = {
    "key": TEST_KEY,
    "repo": "interviewstreet/pair-be",
    "pr": 62,
    "title": "Move the review build off the laptop and onto the workspace VM",
    "base_sha": "9f2c41ab77d3e6058b1c4a9de20f3b7c6d815e4a",
    "head_sha": "3453326da3791b0f4c8e2d5a6079bb31e4c7f8a2",
}

TEST_TOKEN = "test-bearer-token-abc"


# --------------------------------------------------------------------------- #
#  Fake runner
# --------------------------------------------------------------------------- #


@dataclass
class FakeRunner:
    """Return canned replies per section. Optionally block on an event.

    ``replies`` maps a section name to a list of successive replies; each
    ``run(section, ...)`` call pops the next one, so a validation retry can
    hand a different reply the second time.
    """

    replies: dict[str, list[str]] = field(default_factory=dict)
    block_event: threading.Event | None = None
    started: threading.Event = field(default_factory=threading.Event)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def run(self, section: str, prompt: str) -> str:
        self.calls.append((section, prompt))
        self.started.set()
        if self.block_event is not None:
            self.block_event.wait(timeout=5.0)
        remaining = self.replies.get(section) or []
        if not remaining:
            raise RuntimeError(f"no fake reply queued for {section}")
        return remaining.pop(0)


def default_writer_replies() -> dict[str, list[str]]:
    """Well-formed replies for every writer, using refs from EVIDENCE_REFS."""
    return {
        "side_effects": [json.dumps([
            {"text": "Deletes /tmp/pair-cache aggressively.",
             "refs": ["hunk:src/pair/service/jobs.py:7"]}
        ])],
        "plan_vs_result": [json.dumps([
            {"text": "Plan promised streaming but result loads all at once.",
             "refs": ["pr:body"]}
        ])],
        "pushback": [json.dumps([
            {"text": "User asked twice to reconsider two-job cap.",
             "refs": ["pr:body"]}
        ])],
        "summary": [json.dumps({
            "summary": [
                {"text": "Moves the review builder to a service VM behind a signed bundle.",
                 "refs": ["pr:body"]}
            ],
            "look_first": [
                {"text": "Where the tar-extract limits are enforced.",
                 "refs": ["hunk:src/pair/service/jobs.py:7"]}
            ],
        })],
    }


# --------------------------------------------------------------------------- #
#  Payload builder
# --------------------------------------------------------------------------- #


EVIDENCE_REFS = ["pr:body", "hunk:src/pair/service/jobs.py:7"]


def default_evidence(meta: dict) -> dict:
    """A minimal evidence.json that section_inputs is happy to walk."""
    return {
        "pr": {
            "repo": meta["repo"],
            "number": meta["pr"],
            "title": meta["title"],
            "body": "The PR body used by tests to seed the writer inputs.",
        },
        "window": ["2026-09-01T00:00:00Z", "2026-09-28T00:00:00Z"],
        "files": [{"path": "src/pair/service/jobs.py", "additions": 42, "deletions": 3}],
        "sessions": [],
        "related": [],
        "refs": EVIDENCE_REFS,
        "risky_hunks": [{
            "ref": "hunk:src/pair/service/jobs.py:7",
            "tags": ["delete"],
            "added": ["shutil.rmtree(pair_cache)"],
        }],
        "plans": [{"text": "Move the build to the service VM.", "ref": "pr:body"}],
        "final_messages": ["done"],
        "pushback_candidates": [{
            "ref": "pr:body",
            "prompt": "Please reconsider the two-job cap.",
            "interrupted": False,
            "interrupted_before": False,
            "previous_agent_message": "I chose 2 to keep memory bounded.",
            "score": 1.0,
            "at": "2026-09-27T10:00:00Z",
        }],
        "sections": {"decisions": [], "ask": [], "churn": [],
                     "verification": [], "loose_ends": []},
        "files_parsed": 1,
    }


def default_brief(meta: dict) -> dict:
    return {
        "pr": {
            "repo": meta["repo"],
            "number": meta["pr"],
            "title": meta["title"],
            "url": f"https://github.com/{meta['repo']}/pull/{meta['pr']}",
            "head_branch": "feature/service",
            "base_branch": "main",
            "body": "The PR body used by tests to seed the writer inputs.",
        },
        "head_sha": meta["head_sha"],
        "sessions": [],
        "sections": {
            "decisions": [],
            "ask": [],
            "churn": [],
            "verification": [],
            "loose_ends": [],
        },
        "pending": ["side_effects", "plan_vs_result", "pushback", "summary", "look_first"],
        "created_at": "2026-09-28T00:12:00Z",
        "updated_at": "2026-09-28T00:12:00Z",
    }


def build_payload(
    *,
    meta: dict | None = None,
    evidence: dict | None = None,
    brief: dict | None = None,
    extra: dict[str, bytes] | None = None,
    include_meta: bool = True,
) -> bytes:
    """Build a tar.gz payload that :func:`extract_safe` will accept."""
    meta = meta or TEST_META
    evidence = evidence or default_evidence(meta)
    brief = brief or default_brief(meta)
    diff_patch = (
        "diff --git a/src/pair/service/jobs.py b/src/pair/service/jobs.py\n"
        "@@ -1,3 +1,7 @@\n"
        "+import shutil\n"
        "+shutil.rmtree(pair_cache)\n"
    )
    files: dict[str, bytes] = {
        "brief.json": json.dumps(brief, indent=1).encode(),
        "brief.md": b"# placeholder\n",
        "evidence.json": json.dumps(evidence, indent=1).encode(),
        "diff.patch": diff_patch.encode(),
        "risky_hunks.json": json.dumps(evidence["risky_hunks"]).encode(),
    }
    if include_meta:
        files["meta.json"] = json.dumps(meta).encode()
    if extra:
        files.update(extra)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
#  Waiters / helpers
# --------------------------------------------------------------------------- #


def run_and_wait(manager: JobManager, key: str, timeout: float = 10.0):
    """Wait for a specific job to finish. Fails the test if it doesn't."""
    job = manager.get(key)
    assert job is not None, f"no job for {key}"
    result = job.wait(timeout)
    assert result is not None, f"job {key} did not finish within {timeout}s"
    return result


def wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def auth_headers(token: str = TEST_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
