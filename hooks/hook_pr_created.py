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

from paircli_onboarding import approval_command, start as start_paircli_onboarding

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
        contents = cfg.read_text()
        try:
            import tomllib
        except ImportError:
            host_match = re.search(r'^host\s*=\s*"([^"]+)"\s*$', contents, re.MULTILINE)
            port_match = re.search(r"^port\s*=\s*(\d+)\s*$", contents, re.MULTILINE)
            host = host_match.group(1) if host_match else host
            port = int(port_match.group(1)) if port_match else port
        else:
            data = tomllib.loads(contents)
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
    if response is None:
        return None
    stdout = response.get("stdout", "") if isinstance(response, dict) else response
    urls = PR_URL.findall(stdout) if isinstance(stdout, str) else []
    if len(urls) != 1:
        return None
    owner, repo, n = urls[0]
    return f"{owner}/{repo}".lower(), int(n)


def onboarding_message(onboarding: dict, harness: str) -> str:
    status = onboarding.get("status", "unknown")
    if status == "consent_required":
        return (
            "Pair setup needs your approval before it changes anything. It will download Pair CLI, install a hook "
            f"only for {harness}, read local {harness} session data for Pair, and open browser login. "
            "No PR artifact upload is enabled yet. Run: " + onboarding["command"]
        )
    if status == "failed":
        log = onboarding.get("log") or "the Pair plugin data directory"
        return f"Pair setup previously failed: {onboarding.get('error', 'unknown error')}. Log: {log}. Re-run setup: {approval_command(harness)}"
    if status == "started":
        return f"Pair setup started for {harness}; browser login may open shortly. Log: {onboarding.get('log', 'unavailable')}"
    if status == "running":
        return f"Pair setup is already running. Log: {onboarding.get('log', 'unavailable')}"
    if status == "complete":
        return f"Pair CLI is ready for {harness}."
    return f"Pair setup status: {status}."


def write_debug_event(event: dict) -> None:
    destination = os.environ.get("PAIR_HOOK_EVENT_LOG")
    if not destination:
        return
    try:
        safe_event = {
            key: event.get(key)
            for key in ("session_id", "hook_event_name", "tool_name", "tool_use_id")
            if event.get(key) is not None
        }
        safe_event["has_tool_response"] = event.get("tool_response") is not None
        Path(destination).write_text(json.dumps(safe_event, indent=2) + "\n")
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
    is_codex = bool(os.environ.get("PLUGIN_ROOT"))
    harness = "codex" if is_codex else "claude-code"
    onboarding = start_paircli_onboarding(harness, dry_run)
    message = onboarding_message(onboarding, harness)

    if is_codex:
        message += " Pair review artifact upload is not implemented for Codex yet."
        out = {"systemMessage": message,
               "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}}
        if dry_run:
            out["onboarding"] = onboarding
        print(json.dumps(out))
        return 0

    if onboarding.get("status") in {"consent_required", "failed", "unsupported"}:
        print(json.dumps({"systemMessage": message}))
        return 0

    try:
        plan = launch(pr[0], pr[1], root, dry_run)
    except OSError as e:
        print(json.dumps({"systemMessage": f"pair: couldn't start the review brief for {pr[0]}#{pr[1]}: {e}"}))
        return 0
    out = {"systemMessage": f"{message} Building the review brief for {pr[0]}#{pr[1]} at {plan['url']}",
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
