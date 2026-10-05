#!/usr/bin/env python3
"""PostToolUse hook: after `gh pr create` prints a PR URL, build its review brief with a headless /pair run.

Standard library only, and compatible with the macOS system Python, because hooks run with whatever
`python3` is on PATH.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from paircli_onboarding import start as start_paircli_onboarding

GH_PR_CREATE = re.compile(r"(?:^|[\s;&|(/])gh\s+pr\s+create\b")
PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([\w.-]+)/pull/(\d+)")
ALLOWED_TOOLS = ["Bash(pair-brief *)", "Read(~/.pair/briefs/**)", "Edit(~/.pair/briefs/**)",
                 "Write(~/.pair/briefs/**)"]


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
    response = event.get("tool_response")
    if isinstance(response, dict) and response.get("interrupted"):
        return None
    payload = response if response is not None else event
    urls = PR_URL.findall(json.dumps(payload))
    if not urls:
        return None
    owner, repo, n = urls[-1]
    return f"{owner}/{repo}".lower(), int(n)


def write_debug_event(event: dict) -> None:
    destination = os.environ.get("PAIR_HOOK_EVENT_LOG")
    if not destination:
        return
    try:
        Path(destination).write_text(json.dumps(event, indent=2) + "\n")
    except OSError:
        pass


def launch(repo: str, n: int, plugin_root: str, dry_run: bool) -> dict:
    key = f"{repo.replace('/', '__')}__{n}"
    logs = pair_home() / "logs"
    log_path = logs / f"pair-{key}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    # User settings and MCP servers are skipped: they slow every turn and aren't needed to write a brief.
    cmd = [shutil.which("claude") or "claude", "-p", f"/pair {repo}#{n}", "--plugin-dir", plugin_root,
           "--no-session-persistence", "--setting-sources", "project", "--strict-mcp-config",
           "--output-format", "json", "--allowedTools", *ALLOWED_TOOLS]
    env = {k: v for k, v in os.environ.items() if not (k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_"))}
    env.update(MAX_THINKING_TOKENS="0", PAIR_RUN="1")
    plan = {"key": key, "cmd": cmd, "cwd": str(pair_home()), "log": str(log_path),
            "url": f"{base_url()}/review/?key={key}"}
    if dry_run:
        return plan
    logs.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        subprocess.Popen(cmd, cwd=str(pair_home()), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    return plan


def main() -> int:
    if os.environ.get("PAIR_RUN") == "1" or os.environ.get("PAIR_HOOK_DISABLE") == "1":
        return 0
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if isinstance(event, dict):
        write_debug_event(event)
    pr = pr_from_event(event if isinstance(event, dict) else {})
    if pr is None:
        return 0
    root = (os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("PLUGIN_ROOT")
            or str(Path(__file__).resolve().parent.parent))
    dry_run = os.environ.get("PAIR_HOOK_DRY_RUN") == "1"
    onboarding = start_paircli_onboarding(dry_run)
    is_codex = bool(os.environ.get("PLUGIN_ROOT"))

    if is_codex:
        message = f"pair: Pair CLI onboarding is {onboarding['status']}."
        out = {"systemMessage": message,
               "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}}
        if dry_run:
            out["onboarding"] = onboarding
        print(json.dumps(out))
        return 0

    try:
        plan = launch(pr[0], pr[1], root, dry_run)
    except OSError as e:
        print(json.dumps({"systemMessage": f"pair: couldn't start the review brief for {pr[0]}#{pr[1]}: {e}"}))
        return 0
    out = {"systemMessage": f"pair: building the review brief for {pr[0]}#{pr[1]} at {plan['url']}",
           "hookSpecificOutput": {"hookEventName": "PostToolUse",
                                  "additionalContext": f"A review brief for this PR is being built in the "
                                                       f"background. Review page: {plan['url']}"}}
    if dry_run:
        out["plan"] = plan
        out["onboarding"] = onboarding
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
