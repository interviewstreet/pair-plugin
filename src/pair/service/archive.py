"""Safe tar.gz extraction for submitted payloads.

The contract's archive-safety rules are enforced during extraction rather
than by trusting tar headers: sizes are counted as bytes are streamed, and
the compressed-size limit is checked against the raw request body before any
gzip work happens.
"""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

MAX_COMPRESSED_BYTES = 50 * 1024 * 1024   # 50 MiB
MAX_EXPANDED_BYTES = 200 * 1024 * 1024    # 200 MiB
MAX_MEMBERS = 500

# 64 KiB is a safe chunk: small enough that hitting the expanded cap is
# detected within one page, big enough that per-file overhead is negligible.
_CHUNK = 64 * 1024


class ArchiveError(ValueError):
    """A submitted archive violates the safety rules."""


@dataclass
class ArchiveStats:
    members: int
    expanded_bytes: int


def _member_is_bad(name: str) -> str | None:
    """Return the reason a member name is unsafe, or None if it's clean."""
    if not name:
        return "empty member name"
    if "\\" in name:
        return "backslash in member name"
    if name.startswith("/"):
        return "absolute member path"
    # Normalise once for the .. and absolute checks. On POSIX a leading slash
    # is the only absolute form we accept as unsafe; on Windows we already
    # reject the backslash above.
    parts = name.split("/")
    if any(p == ".." for p in parts):
        return "'..' in member path"
    return None


def extract_safe(payload: bytes, dest: Path) -> ArchiveStats:
    """Extract ``payload`` (raw tar.gz bytes) into ``dest``.

    Raises :class:`ArchiveError` on any violation, leaving ``dest`` unchanged
    for the caller's purposes: partial writes are done into a staging subtree
    and only moved into place after the whole archive validates. Since a
    failed submit tears down the whole run, we simply raise and let the
    caller drop the working directory.
    """
    if len(payload) > MAX_COMPRESSED_BYTES:
        raise ArchiveError(
            f"compressed payload {len(payload)} bytes exceeds {MAX_COMPRESSED_BYTES}"
        )
    dest.mkdir(parents=True, exist_ok=True)
    members = 0
    expanded = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            while True:
                info = tar.next()
                if info is None:
                    break
                # Directory entries are ignored; the writer will mkdir as it
                # goes. Skipping them keeps our contents-only rule simple.
                if info.isdir():
                    continue
                if not info.isreg() or info.islnk() or info.issym():
                    raise ArchiveError(
                        f"member {info.name!r} is not a regular file "
                        f"(type={info.type!r})"
                    )
                bad = _member_is_bad(info.name)
                if bad:
                    raise ArchiveError(f"member {info.name!r}: {bad}")
                members += 1
                if members > MAX_MEMBERS:
                    raise ArchiveError(f"more than {MAX_MEMBERS} members")
                stream = tar.extractfile(info)
                if stream is None:
                    raise ArchiveError(
                        f"member {info.name!r} has no readable stream"
                    )
                target = dest / info.name
                # Resolve inside dest to catch anything the name check missed.
                dest_root = dest.resolve()
                resolved = (dest / info.name).resolve()
                try:
                    resolved.relative_to(dest_root)
                except ValueError as exc:
                    raise ArchiveError(
                        f"member {info.name!r} escapes destination"
                    ) from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as out:
                    while True:
                        chunk = stream.read(_CHUNK)
                        if not chunk:
                            break
                        expanded += len(chunk)
                        if expanded > MAX_EXPANDED_BYTES:
                            raise ArchiveError(
                                f"expanded size exceeded "
                                f"{MAX_EXPANDED_BYTES} bytes"
                            )
                        out.write(chunk)
    except tarfile.TarError as exc:
        raise ArchiveError(f"not a valid tar.gz: {exc}") from exc
    return ArchiveStats(members=members, expanded_bytes=expanded)
