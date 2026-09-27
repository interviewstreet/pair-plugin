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
    return f"{repo.replace('/', '__')}__{number}"


def parse_review_key(key: str) -> tuple[str, int]:
    m = re.fullmatch(r"([A-Za-z0-9-]+)__([\w.-]+)__(\d+)", key)
    if not m:
        raise ValueError(f"bad review key: {key!r}")
    return f"{m[1]}/{m[2]}".lower(), int(m[3])
