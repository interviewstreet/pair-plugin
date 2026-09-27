"""Claude command and runner for the service.

The command is unit-tested through :func:`build_command`. The runner is
injected into the job manager, so the tests never invoke the real Claude CLI.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

WRITERS = {
    "side_effects": "pair-side-effects",
    "plan_vs_result": "pair-plan-check",
    "pushback": "pair-pushback",
    "summary": "pair-summary",
}

REPLY_INSTRUCTION = (
    "\n\n---\n\n"
    "Reply with only the JSON list described above. No prose, no markdown, "
    "no code fences. The reply must parse as JSON."
)

# The summary writer produces two sections in one call. The plugin catches
# them via the Write tool, but the service runs Claude with ``--tools ""`` so
# the writer must instead return one object with both lists.
SUMMARY_REPLY_INSTRUCTION = (
    "\n\n---\n\n"
    "Reply with only a JSON object of the shape "
    '{"summary": [...], "look_first": [...]}. Each value is the JSON list '
    "described above for that section. No prose, no markdown, no code fences. "
    "The reply must parse as JSON."
)


def strip_frontmatter(md: str) -> str:
    """Drop the YAML-ish frontmatter block from an ``agents/*.md`` file."""
    if md.startswith("---\n"):
        end = md.find("\n---\n", 4)
        if end != -1:
            return md[end + len("\n---\n"):].lstrip()
    return md


def load_agent_prompt(agents_dir: Path, section: str) -> str:
    """Return the writer agent's prompt body (no frontmatter) for ``section``."""
    writer = WRITERS[section]
    body = (agents_dir / f"{writer}.md").read_text()
    return strip_frontmatter(body).strip()


def _prompt_body(agent_body: str, input_text: str, retry_hint: str | None) -> list[str]:
    parts = [agent_body.strip(), "## Input", input_text.strip()]
    if retry_hint:
        parts.append("## Previous attempt failed with")
        parts.append(retry_hint.strip())
    return parts


def build_prompt(
    agent_body: str,
    input_text: str,
    retry_hint: str | None = None,
    *,
    section: str | None = None,
) -> str:
    """The prompt sent to Claude for one section, optionally with a retry hint.

    ``section`` selects the reply instruction: the summary writer produces
    both ``summary`` and ``look_first`` in one call, so it gets an object
    instruction; every other section replies with a single JSON list.
    """
    body = _prompt_body(agent_body, input_text, retry_hint)
    tail = SUMMARY_REPLY_INSTRUCTION if section == "summary" else REPLY_INSTRUCTION
    return "\n\n".join(body) + tail


def build_command(prompt: str) -> tuple[list[str], dict[str, str]]:
    """The exact argv and env additions the runner uses.

    Kept behind a function so tests can pin the wire format without invoking
    Claude. The env dict is *additions*: the runner merges it into
    ``os.environ`` before spawning the subprocess.
    """
    cmd = [
        "claude", "-p", prompt,
        "--model", "sonnet",
        "--output-format", "json",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--tools", "",
    ]
    env = {"MAX_THINKING_TOKENS": "0"}
    return cmd, env


class ClaudeRunner(Protocol):
    """The interface the job pipeline calls."""

    def run(self, section: str, prompt: str) -> str: ...


class LoginCheck(Protocol):
    def __call__(self) -> bool: ...


@dataclass
class SubprocessRunner:
    """Runs the real ``claude`` CLI. Not used in tests."""

    timeout_s: float = 180.0

    def run(self, section: str, prompt: str) -> str:
        cmd, extra_env = build_command(prompt)
        env = {**os.environ, **extra_env}
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=self.timeout_s)
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude {section} exited {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}"
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"claude {section} returned non-JSON output: {e}") from e
        return (payload.get("result") or "").strip()


def default_login_check() -> bool:
    """Return true only when Claude Code reports an authenticated profile."""
    try:
        proc = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    if proc.returncode != 0:
        return False
    try:
        status = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    return status.get("loggedIn") is True


FakeRunner = Callable[[str, str], str]
