import importlib.util
import sys
from pathlib import Path
from subprocess import CompletedProcess


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
SPEC = importlib.util.spec_from_file_location("pair_notification", HOOKS_DIR / "pair_notification.py")
assert SPEC and SPEC.loader
notifications = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = notifications
SPEC.loader.exec_module(notifications)


PR_URL = "https://github.com/interviewstreet/pair-poc/pull/5"


def test_notify_scan_result_uses_macos_notification(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(notifications.shutil, "which", lambda command: "/usr/bin/osascript")
    monkeypatch.setattr(
        notifications.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or CompletedProcess(command, 0),
    )

    assert notifications.notify_scan_result(PR_URL, succeeded=True, uploaded=True)
    assert calls[0][0][-2] == "Pair review ready"
    assert "PR #5 artifacts uploaded" in calls[0][0][-1]
    assert "https://pair.sh/interviewstreet/pair-poc/pull/5" in calls[0][0][-1]


def test_notify_scan_result_is_a_safe_noop_without_desktop_support(monkeypatch):
    monkeypatch.setattr(notifications.platform, "system", lambda: "Other")

    assert not notifications.notify_scan_result(PR_URL, succeeded=False)
