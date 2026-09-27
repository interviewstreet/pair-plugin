import dataclasses
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DOC_FILE = re.compile(r"(\.md$|(^|/)docs/)", re.I)


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def ts_ms(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def is_doc(path: str) -> bool:
    return bool(DOC_FILE.search(path))


def to_jsonable(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(to_jsonable(x) for x in obj)
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    return obj


def dumps(obj, indent: int | None = 1) -> str:
    return json.dumps(to_jsonable(obj), indent=indent, ensure_ascii=False)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def parse_pr_spec(spec: str) -> tuple[str, int]:
    """'owner/repo#N', 'owner/repo/pull/N', or a github.com PR URL -> ('owner/repo', N)."""
    spec = spec.strip()
    m = (re.match(r"^(?:https?://github\.com/)?([\w.-]+/[\w.-]+)(?:#|/pulls?/)(\d+)", spec)
         or re.match(r"^([\w.-]+/[\w.-]+)\s+(\d+)$", spec))
    if not m:
        raise ValueError(f"not a PR reference: {spec!r} (expected owner/repo#N or a PR URL)")
    return m[1].lower(), int(m[2])


def review_key(repo: str, number: int) -> str:
    """Local-brief key (no head SHA). Existing brief directories still use this."""
    return f"{repo.replace('/', '__')}__{number}"


def parse_review_key(key: str) -> tuple[str, int]:
    m = re.fullmatch(r"([A-Za-z0-9-]+)__([\w.-]+)__(\d+)", key)
    if not m:
        raise ValueError(f"bad review key: {key!r}")
    return f"{m[1]}/{m[2]}".lower(), int(m[3])


# The contract requires each of owner and repo to contain at most a single underscore, so a submit
# key always splits on `__` into exactly four parts (owner, repo, PR number, head SHA prefix).
_KEY_NAME_RX = re.compile(r"[A-Za-z0-9._-]+")
_KEY_SHA_RX = re.compile(r"[0-9a-f]{12}")


def submit_key(repo: str, number: int, head_sha: str) -> str:
    """Service-side review key: <owner>__<repo>__<n>__<head_sha[:12]> (12 lowercase hex chars)."""
    sha = (head_sha or "").lower()
    if not re.fullmatch(r"[0-9a-f]{12,}", sha):
        raise ValueError(f"head_sha must be at least 12 hex chars: {head_sha!r}")
    if "/" not in repo:
        raise ValueError(f"repo must be owner/name: {repo!r}")
    owner, name = repo.split("/", 1)
    for part in (owner, name):
        if "__" in part or not _KEY_NAME_RX.fullmatch(part):
            raise ValueError(f"owner/repo name is not key-safe: {part!r}")
    if number <= 0:
        raise ValueError(f"PR number must be positive: {number!r}")
    return f"{owner}__{name}__{number}__{sha[:12]}"


def parse_submit_key(key: str) -> tuple[str, int, str]:
    """Split a submit key on `__` into exactly four parts, per the frozen contract."""
    parts = key.split("__")
    if len(parts) != 4:
        raise ValueError(f"bad submit key (expected 4 __-separated parts): {key!r}")
    owner, name, num_s, sha = parts
    if not (_KEY_NAME_RX.fullmatch(owner) and _KEY_NAME_RX.fullmatch(name)):
        raise ValueError(f"bad owner/repo in key: {key!r}")
    if not re.fullmatch(r"[0-9]+", num_s) or num_s.startswith("0") and num_s != "0":
        raise ValueError(f"bad PR number in key: {key!r}")
    if not _KEY_SHA_RX.fullmatch(sha):
        raise ValueError(f"bad head SHA in key: {key!r}")
    return f"{owner}/{name}".lower(), int(num_s), sha


def is_submit_key(s: str) -> bool:
    try:
        parse_submit_key(s)
        return True
    except ValueError:
        return False
