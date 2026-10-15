"""Best-effort desktop notifications for background Pair processing."""
from __future__ import annotations

import platform
import re
import shutil
import subprocess


NOTIFICATION_TIMEOUT_SECONDS = 5
PR_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]{0,9})/?"
)


def _notification_command(title: str, message: str) -> list[str] | None:
    system = platform.system()
    if system == "Darwin" and (osascript := shutil.which("osascript")):
        script = (
            "on run argv\n"
            "display notification (item 2 of argv) with title (item 1 of argv)\n"
            "end run"
        )
        return [osascript, "-e", script, "--", title, message]
    if system == "Linux" and (notify_send := shutil.which("notify-send")):
        return [notify_send, "--app-name", "Pair", title, message]
    return None


def notify_scan_result(pr_url: str, *, succeeded: bool, uploaded: bool = False) -> bool:
    """Show a best-effort local notification after background PR processing."""
    match = PR_URL.fullmatch(pr_url)
    if match is None:
        return False
    owner, repository, number = match.groups()
    pair_url = f"https://pair.sh/{owner}/{repository}/pull/{number}"
    if succeeded and uploaded:
        title = "Pair review ready"
        message = f"PR #{number} artifacts uploaded. {pair_url}"
    elif succeeded:
        title = "Pair review ready"
        message = f"PR #{number} artifacts generated. {pair_url}"
    else:
        title = "Pair review failed"
        message = f"PR #{number} processing failed. Check the Pair scan log."
    command = _notification_command(title, message)
    if command is None:
        return False
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=NOTIFICATION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
