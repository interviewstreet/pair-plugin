#!/usr/bin/env python3
"""PostToolUse hook: after `gh pr create` prints a PR URL, submit the review brief in the background.

We must wait about ten seconds before running `pair-brief submit`. `gh pr create` is the last
tool call in the current turn, and Claude Code flushes that tool call's result into the session
JSONL *after* PostToolUse hooks return. If we submit immediately, `pair-brief prepare` reads a
stale session file that's missing the create call, and the review is built from an incomplete
picture. Ten seconds is comfortably longer than the flush.

The hook itself returns almost immediately: it spawns a small Python runner that sleeps and
then execs `pair-brief submit <owner/repo>#<n>`. Arguments are always passed as an argv list,
never through a shell string, so we don't have to worry about quoting or command injection.

Standard library only. Compatible with the macOS system Python because hooks run with whatever
`python3` is on PATH.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

GH_PR_CREATE = re.compile(r"(?:^|[\s;&|(/])gh\s+pr\s+create\b")
PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([\w.-]+)/pull/(\d+)")
DEFAULT_DELAY_S = 10.0
# Runner: sleep for argv[1] seconds, then exec argv[2:] in place. Passed with `python3 -c` so
# every argument (delay, pair-brief path, subcommand, PR spec) travels as its own argv entry.
_RUNNER = ("import os, sys, time\n"
           "time.sleep(float(sys.argv[1]))\n"
           "os.execvp(sys.argv[2], sys.argv[2:])\n")


def pair_home() -> Path:
    return Path(os.environ.get("PAIR_HOME") or Path.home() / ".pair")


def base_url() -> str:
    host, port = "127.0.0.1", 7788
    cfg = Path(os.environ.get("PAIR_CONFIG") or Path.home() / ".config/pair/config.toml")
    try:
        import tomllib
        data = tomllib.loads(cfg.read_text())
        host, port = data.get("host", host), int(data.get("port", port))
    except Exception:
        pass
    return f"http://{host}:{port}"


def pr_from_event(event: dict) -> tuple[str, int] | None:
    if event.get("tool_name") != "Bash":
        return None
    command = (event.get("tool_input") or {}).get("command") or ""
    if not GH_PR_CREATE.search(command):
        return None
    resp = event.get("tool_response") or {}
    if not isinstance(resp, dict) or resp.get("interrupted"):
        return None
    urls = PR_URL.findall(str(resp.get("stdout") or ""))
    if not urls:
        return None
    owner, repo, n = urls[-1]
    return f"{owner}/{repo}".lower(), int(n)


def submit_argv(repo: str, n: int, plugin_root: Path) -> list[str]:
    """The exact argv list for `pair-brief submit`. Never a shell string."""
    return [str(plugin_root / "bin" / "pair-brief"), "submit", f"{repo}#{n}"]


def launch(repo: str, n: int, plugin_root: str, dry_run: bool, delay_s: float) -> dict:
    key = f"{repo.replace('/', '__')}__{n}"
    logs = pair_home() / "logs"
    log_path = logs / f"pair-{key}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    argv = submit_argv(repo, n, Path(plugin_root))
    # A short Python one-liner as the intermediary. We can't just `sleep && ...` in the shell
    # because the contract for this hook is "argv list only, never a shell string".
    runner = [sys.executable, "-c", _RUNNER, f"{delay_s:g}", *argv]
    # PAIR_RUN=1 stops the detached submit from firing this same hook on itself if it happens
    # to touch a Bash tool later. We also scrub Claude Code's own env, so the child doesn't
    # think it's still inside the invoking session.
    env = {k: v for k, v in os.environ.items() if not (k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_"))}
    env.update(PAIR_RUN="1")
    plan = {"key": key, "repo": repo, "pr": n, "argv": argv, "delay_s": delay_s, "runner": runner,
            "cwd": str(pair_home()), "log": str(log_path),
            "url": f"{base_url()}/review/?key={key}"}
    if dry_run:
        return plan
    logs.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        subprocess.Popen(runner, cwd=str(pair_home()), env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)
    return plan


def main() -> int:
    if os.environ.get("PAIR_RUN") == "1" or os.environ.get("PAIR_HOOK_DISABLE") == "1":
        return 0
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    pr = pr_from_event(event if isinstance(event, dict) else {})
    if pr is None:
        return 0
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parent.parent)
    dry_run = os.environ.get("PAIR_HOOK_DRY_RUN") == "1"
    try:
        delay_s = float(os.environ.get("PAIR_HOOK_SUBMIT_DELAY_S", DEFAULT_DELAY_S))
    except ValueError:
        delay_s = DEFAULT_DELAY_S
    try:
        plan = launch(pr[0], pr[1], root, dry_run, delay_s)
    except OSError as e:
        print(json.dumps({"systemMessage": f"pair: couldn't submit the review brief for {pr[0]}#{pr[1]}: {e}"}))
        return 0
    who = f"{pr[0]}#{pr[1]}"
    out = {"systemMessage": f"pair: submitting the review brief for {who} in the background",
           "hookSpecificOutput": {"hookEventName": "PostToolUse",
                                  "additionalContext":
                                  f"A review brief for {who} is being submitted in the background "
                                  f"about {plan['delay_s']:g} seconds after `gh pr create` returned; "
                                  f"see the pair service for progress."}}
    if dry_run:
        out["plan"] = plan
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
