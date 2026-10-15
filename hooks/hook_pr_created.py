#!/usr/bin/env python3
"""PostToolUse hook: build or refresh a review after PR creation and branch pushes.

Standard library only, and compatible with the macOS system Python, because hooks run with whatever
`python3` is on PATH.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pair_pr_comment import start_pair_comment
from paircli_onboarding import approval_command, start as start_paircli_onboarding

PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([\w.-]+)/pull/(\d+)")
SHELL_SEPARATORS = {"&", "&&", "|", "||", ";", "(", ")"}
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


def shell_tokens(command: str) -> list[str]:
    """Split a shell command while preserving control operators."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return []


def command_operation(command: str) -> tuple[str, int, list[str]] | None:
    """Return the supported PR operation and its executable token index."""
    tokens = shell_tokens(command)
    for index, token in enumerate(tokens):
        if Path(token).name == "gh" and tokens[index + 1:index + 3] == ["pr", "create"]:
            return "create", index, tokens
    for index, token in enumerate(tokens):
        if Path(token).name != "git":
            continue
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor] not in SHELL_SEPARATORS:
            if tokens[cursor] == "-C" and cursor + 1 < len(tokens):
                cursor += 2
                continue
            if tokens[cursor].startswith("-"):
                cursor += 1
                continue
            break
        if cursor >= len(tokens) or tokens[cursor] != "push":
            continue
        arguments = []
        for argument in tokens[cursor + 1:]:
            if argument in SHELL_SEPARATORS:
                break
            arguments.append(argument)
        if any(argument in {"--all", "--delete", "--mirror", "--tags", "-d"}
               or argument.startswith(":") for argument in arguments):
            continue
        return "push", index, tokens
    return None


def repo_directory_from_event(event: dict, command: str) -> Path:
    """Resolve the repository directory used by the supported command."""
    raw_cwd = event.get("cwd")
    cwd = Path(raw_cwd).expanduser() if isinstance(raw_cwd, str) and raw_cwd else Path.cwd()
    operation = command_operation(command)
    if operation is None:
        return cwd
    _, executable_index, tokens = operation
    if Path(tokens[executable_index]).name == "git":
        for index in range(executable_index + 1, len(tokens) - 1):
            if tokens[index] == "-C":
                candidate = Path(tokens[index + 1]).expanduser()
                return candidate if candidate.is_absolute() else cwd / candidate
            if tokens[index] == "push" or tokens[index] in SHELL_SEPARATORS:
                break
    for index in range(executable_index - 1, -1, -1):
        if tokens[index] == "cd" and index + 1 < executable_index:
            candidate = Path(tokens[index + 1]).expanduser()
            return candidate if candidate.is_absolute() else cwd / candidate
    return cwd


def pr_from_current_branch(repo_dir: Path) -> tuple[str, int] | None:
    """Resolve the open PR associated with the current branch."""
    gh = shutil.which("gh")
    if not gh or not repo_dir.is_dir():
        return None
    try:
        result = subprocess.run(
            [gh, "pr", "view", "--json", "url", "--jq", ".url"],
            cwd=repo_dir,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    urls = PR_URL.findall(result.stdout)
    if result.returncode != 0 or len(urls) != 1:
        return None
    owner, repo, number = urls[0]
    return f"{owner}/{repo}".lower(), int(number)


def pr_from_event(event: dict, repo_dir: Path) -> tuple[str, int] | None:
    if event.get("tool_name") != "Bash":
        return None
    command = (event.get("tool_input") or {}).get("command") or ""
    operation = command_operation(command)
    if operation is None:
        return None
    response = event.get("tool_response")
    if isinstance(response, dict) and response.get("interrupted"):
        return None
    if isinstance(response, dict):
        for key in ("exit_code", "exitCode", "returncode"):
            if isinstance(response.get(key), int) and response[key] != 0:
                return None
    if operation[0] == "push" or response is None:
        return pr_from_current_branch(repo_dir)
    stdout = response.get("stdout", "") if isinstance(response, dict) else response
    urls = PR_URL.findall(stdout) if isinstance(stdout, str) else []
    if len(urls) != 1:
        return None
    owner, repo, n = urls[0]
    return f"{owner}/{repo}".lower(), int(n)


def onboarding_message(onboarding: dict, harness: str) -> str:
    status = onboarding.get("status", "unknown")
    if status == "failed":
        log = onboarding.get("log") or "the Pair plugin data directory"
        return f"Pair setup previously failed: {onboarding.get('error', 'unknown error')}. Log: {log}. Re-run setup: {approval_command(harness)}"
    if status == "started":
        return f"Pair setup started for {harness}; browser login may open shortly. Log: {onboarding.get('log', 'unavailable')}"
    if status == "running":
        return f"Pair setup is already running. Log: {onboarding.get('log', 'unavailable')}"
    if status == "complete":
        scan = onboarding.get("scan")
        if isinstance(scan, dict) and scan.get("status") == "started":
            return f"Pair CLI is ready for {harness}. Generating local Pair artifacts and uploading the ZIP; log: {scan.get('log', 'unavailable')}"
        if isinstance(scan, dict) and scan.get("status") == "failed":
            return f"Pair CLI is ready for {harness}, but local artifact generation failed: {scan.get('error', 'unknown error')}. Log: {scan.get('log', 'unavailable')}"
        return f"Pair CLI is ready for {harness}."
    return f"Pair setup status: {status}."


def pair_pr_url(repo: str, number: int) -> str:
    """Return the Pair review URL corresponding to one GitHub pull request."""
    return f"https://pair.sh/{repo}/pull/{number}"


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
    event = event if isinstance(event, dict) else {}
    command = (event.get("tool_input") or {}).get("command") or ""
    repo_dir = repo_directory_from_event(event, command)
    pr = pr_from_event(event, repo_dir)
    if pr is None:
        return 0
    root = (os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("PLUGIN_ROOT")
            or str(Path(__file__).resolve().parent.parent))
    dry_run = os.environ.get("PAIR_HOOK_DRY_RUN") == "1"
    is_codex = bool(os.environ.get("PLUGIN_ROOT"))
    harness = "codex" if is_codex else "claude-code"
    pr_url = f"https://github.com/{pr[0]}/pull/{pr[1]}"
    review_url = pair_pr_url(pr[0], pr[1])
    comment = start_pair_comment(pr_url, review_url, repo_dir, pair_home() / "logs", dry_run)
    onboarding = start_paircli_onboarding(harness, dry_run, pr_url=pr_url, repo_dir=str(repo_dir))
    message = f"{onboarding_message(onboarding, harness)} Pair review: {review_url}. Posting this link to the PR."

    if is_codex:
        out = {"systemMessage": message,
               "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}}
        if dry_run:
            out["onboarding"] = onboarding
            out["comment"] = comment
        print(json.dumps(out))
        return 0

    if onboarding.get("status") in {"failed", "unsupported"}:
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
                                                       f"background. Pair review: {review_url}. "
                                                       f"Local review page: {plan['url']}"}}
    if dry_run:
        out["plan"] = plan
        out["onboarding"] = onboarding
        out["comment"] = comment
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
