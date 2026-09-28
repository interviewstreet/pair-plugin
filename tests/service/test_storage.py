"""Storage prefix + timestamp path helpers."""

from datetime import datetime, timezone

from pair.service.storage import (
    InMemoryStorage,
    bundle_path,
    manifest_path,
    next_payload_path,
    payload_path,
    payload_ts,
)


def test_manifest_and_bundle_paths_use_reviews_prefix():
    assert manifest_path("k") == "reviews/k/manifest.json"
    assert bundle_path("k") == "reviews/k/bundle.tar.gz"


def test_payload_ts_is_compact_rfc3339_utc():
    ts = payload_ts(datetime(2026, 9, 28, 0, 12, 0, tzinfo=timezone.utc))
    assert ts == "20260928T001200Z"
    assert payload_path("k", ts) == "payloads/k/20260928T001200Z.tar.gz"


def test_next_payload_path_adds_counter_on_collision():
    store = InMemoryStorage()
    now = datetime(2026, 9, 28, 0, 12, 0, tzinfo=timezone.utc)
    first = next_payload_path(store, "k", now=now)
    assert first == "payloads/k/20260928T001200Z.tar.gz"
    store.put_bytes(first, b"x")
    second = next_payload_path(store, "k", now=now)
    assert second == "payloads/k/20260928T001200Z-2.tar.gz"
    store.put_bytes(second, b"x")
    third = next_payload_path(store, "k", now=now)
    assert third == "payloads/k/20260928T001200Z-3.tar.gz"


def test_in_memory_object_url_needs_object():
    store = InMemoryStorage()
    store.put_bytes("reviews/k/bundle.tar.gz", b"hi")
    url = store.object_url("reviews/k/bundle.tar.gz")
    assert url == "https://storage.googleapis.com/memory/reviews/k/bundle.tar.gz"
    assert "?" not in url
