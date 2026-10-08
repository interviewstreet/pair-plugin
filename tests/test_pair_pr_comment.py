import importlib.util
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
SPEC = importlib.util.spec_from_file_location("pair_pr_comment", HOOKS_DIR / "pair_pr_comment.py")
assert SPEC and SPEC.loader
comments = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = comments
SPEC.loader.exec_module(comments)


PR_URL = "https://github.com/interviewstreet/pair-poc/pull/5"
PAIR_URL = "https://pair.sh/interviewstreet/pair-poc/pull/5"


def test_upsert_pair_comment_creates_marker_comment(monkeypatch, tmp_path):
    calls = []
    responses = iter([
        CompletedProcess([], 0, "dhruvi\n", ""),
        CompletedProcess([], 0, "[]", ""),
        CompletedProcess([], 0, "{}", ""),
    ])
    monkeypatch.setattr(comments.shutil, "which", lambda command: "/usr/bin/gh")
    monkeypatch.setattr(
        comments,
        "_run_gh",
        lambda gh, arguments, cwd: calls.append((gh, arguments, cwd)) or next(responses),
    )

    comments.upsert_pair_comment(PR_URL, PAIR_URL, tmp_path)

    assert calls[-1][1][:4] == [
        "api",
        "--method",
        "POST",
        "repos/interviewstreet/pair-poc/issues/5/comments",
    ]
    assert PAIR_URL in calls[-1][1][-1]
    assert comments.COMMENT_MARKER in calls[-1][1][-1]


def test_upsert_pair_comment_updates_own_marker_comment(monkeypatch, tmp_path):
    calls = []
    existing = [{"id": 42, "body": comments.COMMENT_MARKER, "user": {"login": "Dhruvi"}}]
    responses = iter([
        CompletedProcess([], 0, "dhruvi\n", ""),
        CompletedProcess([], 0, json.dumps(existing), ""),
        CompletedProcess([], 0, "{}", ""),
    ])
    monkeypatch.setattr(comments.shutil, "which", lambda command: "/usr/bin/gh")
    monkeypatch.setattr(
        comments,
        "_run_gh",
        lambda gh, arguments, cwd: calls.append((gh, arguments, cwd)) or next(responses),
    )

    comments.upsert_pair_comment(PR_URL, PAIR_URL, tmp_path)

    assert calls[-1][1][:4] == [
        "api",
        "--method",
        "PATCH",
        "repos/interviewstreet/pair-poc/issues/comments/42",
    ]
