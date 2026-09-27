"""Job pipeline: manifest transitions, ordering, validation retry, two-job cap."""

import io
import json
import tarfile
import threading
import time

from pair.service.jobs import JobManager, PARALLEL_SECTIONS
from pair.service.manifest import SECTION_NAMES
from pair.service.storage import (
    InMemoryStorage,
    bundle_path,
    manifest_path,
    review_object,
)

from .helpers import (
    AGENTS_DIR,
    FakeRunner,
    TEST_KEY,
    TEST_META,
    build_payload,
    default_writer_replies,
    run_and_wait,
    wait_until,
)


def _start_job(manager: JobManager, storage: InMemoryStorage, payload_bytes: bytes) -> str:
    path = manager.store_payload(TEST_KEY, payload_bytes)
    manager.start(TEST_KEY, TEST_META, payload_ref=path)
    return path


def test_happy_path_writes_bundle_only_after_all_sections_done(storage, manager):
    payload = build_payload()
    _start_job(manager, storage, payload)
    result = run_and_wait(manager, TEST_KEY)
    assert result.status == "done"

    manifest = json.loads(storage.get_bytes(manifest_path(TEST_KEY)).decode())
    assert manifest["status"] == "done"
    for name in SECTION_NAMES:
        assert manifest["sections"][name]["status"] == "done"
        assert manifest["sections"][name]["error"] is None

    # Bundle exists and every write of it came before the final manifest flip.
    bundle_p = bundle_path(TEST_KEY)
    manifest_p = manifest_path(TEST_KEY)
    assert bundle_p in storage.blobs
    # The last write must be the manifest (the "flip to done" step).
    assert storage.writes[-1] == manifest_p
    # And bundle.tar.gz must appear before that final manifest write.
    last_bundle = max(i for i, p in enumerate(storage.writes) if p == bundle_p)
    last_manifest = max(i for i, p in enumerate(storage.writes) if p == manifest_p)
    assert last_bundle < last_manifest, "manifest must flip to done AFTER bundle upload"


def test_manifest_appears_first_and_bundle_appears_before_final_flip(storage, manager):
    _start_job(manager, storage, build_payload())
    run_and_wait(manager, TEST_KEY)
    manifest_p = manifest_path(TEST_KEY)
    bundle_p = bundle_path(TEST_KEY)
    # First manifest write comes before any bundle write.
    first_manifest_idx = storage.writes.index(manifest_p)
    assert bundle_p not in storage.writes[:first_manifest_idx + 1]
    # Bundle appears exactly once, and the manifest is rewritten after it.
    bundle_indices = [i for i, p in enumerate(storage.writes) if p == bundle_p]
    assert len(bundle_indices) == 1, storage.writes
    manifest_after_bundle = [
        i for i, p in enumerate(storage.writes)
        if p == manifest_p and i > bundle_indices[0]
    ]
    assert manifest_after_bundle, (
        "no manifest rewrite after bundle upload; the done flip is missing"
    )


def test_bundle_contains_no_raw_jsonl_and_uses_done_manifest(storage, manager):
    # Include a raw JSONL "session" file in the payload; the bundle must strip it.
    jsonl = b'{"role":"user","text":"hello"}\n'
    payload = build_payload(extra={"sessions/claude-abc.jsonl": jsonl})
    _start_job(manager, storage, payload)
    run_and_wait(manager, TEST_KEY)
    bundle_bytes = storage.get_bytes(bundle_path(TEST_KEY))
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tar:
        names = tar.getnames()
        assert all(not n.endswith(".jsonl") for n in names), names
        assert "manifest.json" in names
        packed_manifest = json.loads(tar.extractfile("manifest.json").read().decode())
    assert packed_manifest["status"] == "done"
    # The packed manifest and the flipped prefix manifest agree.
    prefix_manifest = json.loads(storage.get_bytes(manifest_path(TEST_KEY)).decode())
    assert packed_manifest == prefix_manifest


def test_validation_retry_recovers_and_marks_done(storage):
    # First reply for pushback is not JSON; the retry hands a good list.
    replies = default_writer_replies()
    replies["pushback"] = [
        "not json at all",
        json.dumps([{"text": "Second try.", "refs": ["pr:body"]}]),
    ]
    runner = FakeRunner(replies=replies)
    manager = JobManager(
        storage=storage, runner=runner, agents_dir=AGENTS_DIR,
        login_check=lambda: True, max_concurrent=2,
    )
    _start_job(manager, storage, build_payload())
    run_and_wait(manager, TEST_KEY)
    manifest = json.loads(storage.get_bytes(manifest_path(TEST_KEY)).decode())
    assert manifest["sections"]["pushback"]["status"] == "done"
    # The runner was called twice for pushback.
    assert sum(1 for s, _ in runner.calls if s == "pushback") == 2


def test_validation_fails_after_one_retry_marks_section_and_job_failed(storage):
    replies = default_writer_replies()
    replies["pushback"] = ["nonsense", "still nonsense"]
    runner = FakeRunner(replies=replies)
    manager = JobManager(
        storage=storage, runner=runner, agents_dir=AGENTS_DIR,
        login_check=lambda: True, max_concurrent=2,
    )
    _start_job(manager, storage, build_payload())
    result = run_and_wait(manager, TEST_KEY)
    assert result.status == "failed"
    manifest = json.loads(storage.get_bytes(manifest_path(TEST_KEY)).decode())
    assert manifest["status"] == "failed"
    assert manifest["sections"]["pushback"]["status"] == "failed"
    assert manifest["sections"]["pushback"]["error"]  # non-empty string
    # No bundle on failure.
    assert bundle_path(TEST_KEY) not in storage.blobs


def test_summary_writer_saves_both_summary_and_look_first(storage, manager, fake_runner):
    _start_job(manager, storage, build_payload())
    run_and_wait(manager, TEST_KEY)
    brief = json.loads(storage.get_bytes(review_object(TEST_KEY, "brief.json")).decode())
    assert brief["sections"]["summary"], "summary section should be populated"
    assert brief["sections"]["look_first"], "look_first section should be populated"


def test_two_job_cap_holds_third_job_behind_the_semaphore(storage):
    """Third job cannot start until one of the first two releases the slot."""
    release = threading.Event()
    replies = default_writer_replies()
    runner = FakeRunner(replies=replies, block_event=release)
    # Every job shares this runner; multiple sections queued won't matter here
    # because the test observes active_count(), not per-section calls.
    manager = JobManager(
        storage=storage, runner=runner, agents_dir=AGENTS_DIR,
        login_check=lambda: True, max_concurrent=2,
    )

    def _key(n):
        # Distinct 12-hex head parts per key to satisfy the key grammar.
        return f"acme__demo__{n}__abcdef0123{n:02x}"

    for i in (1, 2, 3):
        # Give each key its own runner-replies clone so a completing job
        # doesn't drain another job's queue.
        pass

    # Simpler: only spin up jobs that will actually run once released. We
    # can't call ``start`` many times with the same shared FakeRunner because
    # its replies pop from a shared list. Give the runner infinite replies.
    runner.replies = {
        s: [replies[s][0]] * 20 for s in replies
    }
    keys = [_key(i) for i in (1, 2, 3)]
    for k in keys:
        meta = dict(TEST_META, key=k, pr=int(k.split("__")[2]),
                    head_sha=k.split("__")[3] + "0" * (40 - 12))
        # meta.head_sha only needs the first 12 to match the key.
        payload = build_payload(meta=meta)
        path = manager.store_payload(k, payload)
        manager.start(k, meta, payload_ref=path)

    # Wait until at least one runner call has started (so a job is inside the
    # semaphore and blocked on the release event), then assert cap = 2.
    assert wait_until(lambda: manager.active_count() == 2, timeout=5.0), \
        f"active_count never reached 2: was {manager.active_count()}"
    time.sleep(0.1)
    assert manager.active_count() == 2, \
        f"third job should be blocked: active_count={manager.active_count()}"

    release.set()
    # Everyone finishes eventually.
    manager.wait_all(timeout=15.0)
    for k in keys:
        assert manager.get(k) is not None
        assert manager.get(k).result is not None


def test_writer_sections_run_in_parallel_all_start_before_any_finishes(storage):
    """The three parallel writers must all be started concurrently."""
    barrier = threading.Barrier(len(PARALLEL_SECTIONS), timeout=5.0)
    lock = threading.Lock()
    calls: list[str] = []

    class BarrierRunner:
        def __init__(self):
            self.summary_replies = default_writer_replies()["summary"][:]

        def run(self, section, prompt):
            if section in PARALLEL_SECTIONS:
                barrier.wait()
                with lock:
                    calls.append(section)
                return default_writer_replies()[section][0]
            # summary comes later, no barrier.
            return self.summary_replies.pop(0)

    manager = JobManager(
        storage=storage, runner=BarrierRunner(), agents_dir=AGENTS_DIR,
        login_check=lambda: True, max_concurrent=2,
    )
    payload = build_payload()
    path = manager.store_payload(TEST_KEY, payload)
    manager.start(TEST_KEY, TEST_META, payload_ref=path)
    result = run_and_wait(manager, TEST_KEY, timeout=15.0)
    assert result.status == "done"
    assert set(calls) == set(PARALLEL_SECTIONS)


def test_failed_run_leaves_no_bundle_and_marks_error(storage):
    # Runner throws every time for pushback; even the retry raises.
    class BoomRunner:
        def __init__(self):
            self.replies = default_writer_replies()

        def run(self, section, prompt):
            if section == "pushback":
                raise RuntimeError("network exploded")
            return self.replies[section][0]

    manager = JobManager(
        storage=storage, runner=BoomRunner(), agents_dir=AGENTS_DIR,
        login_check=lambda: True, max_concurrent=2,
    )
    path = manager.store_payload(TEST_KEY, build_payload())
    manager.start(TEST_KEY, TEST_META, payload_ref=path)
    result = run_and_wait(manager, TEST_KEY)
    assert result.status == "failed"
    manifest = json.loads(storage.get_bytes(manifest_path(TEST_KEY)).decode())
    assert manifest["status"] == "failed"
    assert manifest["sections"]["pushback"]["error"]
    assert bundle_path(TEST_KEY) not in storage.blobs
