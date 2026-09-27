import concurrent.futures as cf
import os
from datetime import datetime
from functools import partial
from pathlib import Path

from pair.config import claude_dir, codex_dir
from pair.model import Session
from pair.readers.claude import read_claude
from pair.readers.codex import read_codex


def read_any(path: str, messages: bool = False) -> Session | None:
    if os.path.basename(path).startswith("rollout-"):
        return read_codex(path, messages)
    return read_claude(path, messages)


def discover(since: datetime) -> list[str]:
    """Session files modified at or after `since`, across both agents."""
    out = []
    cutoff = since.timestamp()
    for root in (claude_dir(), codex_dir()):
        if not root.exists():
            continue
        for p in root.rglob("*.jsonl"):
            try:
                if p.stat().st_mtime >= cutoff:
                    out.append(str(p))
            except OSError:
                continue
    return out


def _safe_read(path: str, messages: bool) -> Session | None:
    try:
        return read_any(path, messages)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def parse_all(paths: list[str], messages: bool = False) -> list[Session]:
    if len(paths) <= 4:
        return [s for s in (_safe_read(p, messages) for p in paths) if s]
    # Largest files first so one big session doesn't finish last.
    paths = sorted(paths, key=lambda p: -Path(p).stat().st_size)
    with cf.ProcessPoolExecutor() as ex:
        return [s for s in ex.map(partial(_safe_read, messages=messages), paths, chunksize=2) if s]
