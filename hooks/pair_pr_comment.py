#!/usr/bin/env python3
"""Create or update the Pair review link comment on a GitHub pull request."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


COMMENT_MARKER = "<!-- pair-review-link -->"
COMMAND_TIMEOUT_SECONDS = 15
PR_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]{0,9})/?"
)


def comment_body(pair_url: str) -> str:
    """Return the idempotent GitHub comment body for one Pair review."""
    return f"Review this PR in Pair: {pair_url}\n\n{COMMENT_MARKER}"


def _run_gh(gh: str, arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [gh, *arguments],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=True,
    )


def upsert_pair_comment(pr_url: str, pair_url: str, repo_dir: Path) -> None:
    """Create the Pair comment, or update the current user's existing marker comment."""
    match = PR_URL.fullmatch(pr_url)
    if match is None:
        raise ValueError("Pair comments require a GitHub pull-request URL")
    owner, repository, number = match.groups()
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("gh is required to post the Pair review link")

    viewer = _run_gh(gh, ["api", "user", "--jq", ".login"], repo_dir).stdout.strip().casefold()
    response = _run_gh(
        gh,
        ["api", f"repos/{owner}/{repository}/issues/{number}/comments?per_page=100"],
        repo_dir,
    )
    comments = json.loads(response.stdout)
    if not isinstance(comments, list):
        raise RuntimeError("GitHub returned an invalid PR comment list")

    body = comment_body(pair_url)
    existing_id = next(
        (
            comment.get("id")
            for comment in comments
            if isinstance(comment, dict)
            and COMMENT_MARKER in str(comment.get("body", ""))
            and str((comment.get("user") or {}).get("login", "")).casefold() == viewer
            and isinstance(comment.get("id"), int)
        ),
        None,
    )
    if existing_id is None:
        arguments = ["api", "--method", "POST", f"repos/{owner}/{repository}/issues/{number}/comments"]
    else:
        arguments = ["api", "--method", "PATCH", f"repos/{owner}/{repository}/issues/comments/{existing_id}"]
    _run_gh(gh, [*arguments, "-f", f"body={body}"], repo_dir)


def start_pair_comment(
    pr_url: str,
    pair_url: str,
    repo_dir: Path,
    logs_dir: Path,
    dry_run: bool,
) -> dict:
    """Start detached Pair-comment delivery and return its execution plan."""
    match = PR_URL.fullmatch(pr_url)
    if match is None:
        raise ValueError("Pair comments require a GitHub pull-request URL")
    owner, repository, number = match.groups()
    log_path = logs_dir / f"pair-comment-{owner}__{repository}__{number}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    command = [sys.executable, str(Path(__file__).resolve()), pr_url, pair_url, str(repo_dir)]
    plan = {"command": command, "log": str(log_path)}
    if dry_run:
        return plan

    logs_dir.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.Popen(
            command,
            cwd=repo_dir,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return plan


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    try:
        upsert_pair_comment(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"pair: could not post Pair review link: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
