"""Shared reading of snapshot.json's PR object: head binding and line-data availability."""
from __future__ import annotations

import json
from pathlib import Path

# `gh pr view --json commits` returns at most this many commits, so a full list may be cut short.
GITHUB_COMMIT_LIST_LIMIT = 100
MAX_NO_LINE_DATA_PATHS = 50
MAX_QUOTED_PATH_CHARS = 300


def captured_pr(artifact_dir: Path) -> dict:
    """Read snapshot.json's PR object, treating a missing or oddly shaped one as empty."""
    try:
        snapshot = json.loads((artifact_dir / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pr = snapshot.get("pr") if isinstance(snapshot, dict) else None
    return pr if isinstance(pr, dict) else {}


def snapshot_binds_head(pr: dict, head: str) -> bool:
    """True when the captured commits can vouch for `head`.

    GitHub lists only the first 100 commits, so a list that reaches the limit may end before the
    real head and cannot disprove it. A shorter list is complete, so its last commit must be the
    head: a later push would otherwise leave a snapshot of a newer head bound to the scanned one.
    """
    commits = [commit for commit in pr.get("commits") or [] if isinstance(commit, dict)] if isinstance(pr, dict) else []
    if not commits:
        return False
    if len(commits) >= GITHUB_COMMIT_LIST_LIMIT:
        return True
    return commits[-1].get("sha") == head


def lines_available(file: dict) -> bool:
    """A file can be cited by line only when its snapshot entry kept at least one hunk."""
    hunks = file.get("hunks") if isinstance(file, dict) else None
    return isinstance(hunks, list) and any(isinstance(hunk, dict) for hunk in hunks)


def files_without_line_data(pr: dict) -> list[str]:
    """Changed paths with no hunks: the files-API fallback dropped the patch, or binary or rename-only."""
    files = pr.get("files") if isinstance(pr, dict) and isinstance(pr.get("files"), list) else []
    paths = []
    for file in files:
        path = file.get("path") if isinstance(file, dict) else None
        if isinstance(path, str) and path and not lines_available(file) and path not in paths:
            paths.append(path)
    return paths


def count_files(pr: dict) -> int:
    """Number of distinct changed paths in the snapshot."""
    files = pr.get("files") if isinstance(pr, dict) and isinstance(pr.get("files"), list) else []
    return len({file["path"] for file in files if isinstance(file, dict) and isinstance(file.get("path"), str) and file["path"]})


def quote_path(path: str, limit: int = MAX_QUOTED_PATH_CHARS) -> str:
    """A path as one JSON string on one line, cut to `limit` characters, so it cannot end a prompt block."""
    text = path if len(path) <= limit else path[:limit - 3] + "..."
    quoted = json.dumps(text, ensure_ascii=False)
    for separator in ("\x85", "\u2028", "\u2029"):
        quoted = quoted.replace(separator, f"\\u{ord(separator):04x}")
    return quoted


def no_line_data_note(pr: dict, limit: int = MAX_NO_LINE_DATA_PATHS) -> str:
    """One prompt line naming paths that must not be cited with line ranges, or "" when none."""
    paths = files_without_line_data(pr)
    if not paths:
        return ""
    shown = ", ".join(quote_path(path) for path in paths[:limit])
    more = f" and {len(paths) - limit} more" if len(paths) > limit else ""
    return (f"These changed paths have no line data, so never cite them with line ranges: {shown}{more}.")
