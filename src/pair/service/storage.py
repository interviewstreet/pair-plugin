"""Object storage abstraction.

The service writes payloads, per-key manifests, and the final bundle through
this interface. Tests use ``InMemoryStorage``. Downloads use a plain HTTPS
object URL plus a bearer token from the VM metadata server, so the service
never signs a URL.

Path layout matches ``docs/bundle.md``: rendered files live under
``reviews/<key>/`` and archived submits under ``payloads/<key>/<ts>.tar.gz``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol


class ObjectMissing(KeyError):
    """Raised when a lookup on a storage backend cannot find the path."""


class Storage(Protocol):
    """The subset of object-store operations the service uses."""

    def put_bytes(self, path: str, data: bytes) -> None: ...
    def get_bytes(self, path: str) -> bytes: ...
    def exists(self, path: str) -> bool: ...
    def object_url(self, path: str) -> str: ...


@dataclass
class InMemoryStorage:
    """A dict-backed backend for tests."""

    blobs: dict[str, bytes] = field(default_factory=dict)
    # Ordered log of writes, useful when tests need to assert write order.
    writes: list[str] = field(default_factory=list)

    def put_bytes(self, path: str, data: bytes) -> None:
        self.blobs[path] = bytes(data)
        self.writes.append(path)

    def get_bytes(self, path: str) -> bytes:
        if path not in self.blobs:
            raise ObjectMissing(path)
        return self.blobs[path]

    def exists(self, path: str) -> bool:
        return path in self.blobs

    def object_url(self, path: str) -> str:
        if not self.exists(path):
            raise ObjectMissing(path)
        return f"https://storage.googleapis.com/memory/{path}"


class GCSStorage:
    """Wraps a ``google.cloud.storage.Bucket``. Not imported unless used."""

    def __init__(self, bucket) -> None:
        self.bucket = bucket

    def put_bytes(self, path: str, data: bytes) -> None:
        blob = self.bucket.blob(path)
        blob.upload_from_string(data)

    def get_bytes(self, path: str) -> bytes:
        blob = self.bucket.blob(path)
        if not blob.exists():
            raise ObjectMissing(path)
        return blob.download_as_bytes()

    def exists(self, path: str) -> bool:
        return self.bucket.blob(path).exists()

    def object_url(self, path: str) -> str:
        """HTTPS URL for one object. The caller supplies the bearer token."""
        from urllib.parse import quote

        name = getattr(self.bucket, "name", None) or "bucket"
        return "https://storage.googleapis.com/" + quote(name, safe="") + "/" + quote(path, safe="/")


# --- Path helpers ------------------------------------------------------------

REVIEW_PREFIX = "reviews"
PAYLOAD_PREFIX = "payloads"

# Rendered bundle files, per the contract. `manifest.json` is written first
# and last; `bundle.tar.gz` is written after every other file it contains.
BUNDLE_FILES = (
    "manifest.json",
    "brief.json",
    "brief.md",
    "evidence.json",
    "diff.patch",
    "risky_hunks.json",
    "AGENTS.md",
)


def review_prefix(key: str) -> str:
    return f"{REVIEW_PREFIX}/{key}/"


def review_object(key: str, name: str) -> str:
    return f"{REVIEW_PREFIX}/{key}/{name}"


def manifest_path(key: str) -> str:
    return f"{REVIEW_PREFIX}/{key}/manifest.json"


def bundle_path(key: str) -> str:
    return f"{REVIEW_PREFIX}/{key}/bundle.tar.gz"


def payload_ts(now: datetime | None = None) -> str:
    """Compact RFC3339 UTC: ``YYYYMMDDTHHMMSSZ`` (no dashes, no colons)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def payload_path(key: str, ts: str) -> str:
    return f"{PAYLOAD_PREFIX}/{key}/{ts}.tar.gz"


def next_payload_path(storage: Storage, key: str, now: datetime | None = None) -> str:
    """A collision-free ``payloads/<key>/<ts>[-n].tar.gz`` path for this second.

    Two submits for one key arriving in the same UTC second get suffixes
    ``-2``, ``-3``, ... The first submit in that second stays unsuffixed.
    """
    ts = payload_ts(now)
    base = f"{PAYLOAD_PREFIX}/{key}/{ts}"
    candidate = f"{base}.tar.gz"
    if not storage.exists(candidate):
        return candidate
    n = 2
    while storage.exists(f"{base}-{n}.tar.gz"):
        n += 1
    return f"{base}-{n}.tar.gz"


def gce_access_token() -> str:
    """Access token from the GCE metadata server.

    The attached service account can read the review bucket. The token lasts
    about an hour and is not limited to one object.
    """
    import json
    import urllib.request

    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        body = json.load(response)
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("metadata server returned no access token")
    return token
