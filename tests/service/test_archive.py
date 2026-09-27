"""Archive safety: safety enforced during extraction, not from tar headers."""

import gzip
import io
import tarfile

import pytest

from pair.service.archive import (
    MAX_COMPRESSED_BYTES,
    MAX_EXPANDED_BYTES,
    MAX_MEMBERS,
    ArchiveError,
    extract_safe,
)


def _tar_gz(entries) -> bytes:
    """Build a tar.gz with a list of (add_fn, tarinfo, payload_bytes) tuples.

    We drop down to the low-level TarInfo so tests can inject symlinks, bad
    names, or lying header sizes without the tarfile helper getting in the way.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, payload in entries:
            if payload is None:
                tar.addfile(info)
            else:
                tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _regfile(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.REGTYPE
    info.size = len(data)
    info.mode = 0o644
    return info, data


def test_accepts_valid_regular_files(tmp_path):
    payload = _tar_gz([_regfile("a/b.txt", b"hello")])
    stats = extract_safe(payload, tmp_path)
    assert stats.members == 1
    assert (tmp_path / "a" / "b.txt").read_bytes() == b"hello"


def test_rejects_symlink(tmp_path):
    info = tarfile.TarInfo(name="link")
    info.type = tarfile.SYMTYPE
    info.linkname = "target"
    payload = _tar_gz([(info, None)])
    with pytest.raises(ArchiveError, match="regular"):
        extract_safe(payload, tmp_path)


def test_rejects_traversal(tmp_path):
    payload = _tar_gz([_regfile("../escape.txt", b"x")])
    with pytest.raises(ArchiveError, match=r"\.\."):
        extract_safe(payload, tmp_path)


def test_rejects_absolute_path(tmp_path):
    payload = _tar_gz([_regfile("/etc/passwd", b"x")])
    with pytest.raises(ArchiveError, match="absolute"):
        extract_safe(payload, tmp_path)


def test_rejects_backslash(tmp_path):
    payload = _tar_gz([_regfile("windows\\name.txt", b"x")])
    with pytest.raises(ArchiveError, match="backslash"):
        extract_safe(payload, tmp_path)


def test_rejects_too_many_members(tmp_path):
    entries = [_regfile(f"f{i}.txt", b"x") for i in range(MAX_MEMBERS + 1)]
    payload = _tar_gz(entries)
    with pytest.raises(ArchiveError, match="members"):
        extract_safe(payload, tmp_path)


def test_rejects_oversized_compressed_body(tmp_path):
    body = b"\x1f\x8b" + b"\x00" * (MAX_COMPRESSED_BYTES + 1)
    with pytest.raises(ArchiveError, match="compressed"):
        extract_safe(body, tmp_path)


def test_expanded_cap_ignores_header_and_counts_bytes(tmp_path):
    # The header claims a tiny size, but the actual stream is bigger than the
    # expanded cap. Cap must be enforced by counting written bytes.
    data = b"a" * (MAX_EXPANDED_BYTES + 1024)
    info = tarfile.TarInfo(name="big.bin")
    info.type = tarfile.REGTYPE
    info.size = len(data)
    info.mode = 0o644
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=1) as tar:
        tar.addfile(info, io.BytesIO(data))
    payload = buf.getvalue()
    # Payload is well under the compressed cap because 'aaaa' compresses hard.
    assert len(payload) < MAX_COMPRESSED_BYTES
    with pytest.raises(ArchiveError, match="expanded"):
        extract_safe(payload, tmp_path)
