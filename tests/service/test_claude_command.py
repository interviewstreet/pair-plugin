"""build_command argv + reply instructions."""

from types import SimpleNamespace

from pair.service.claude import (
    REPLY_INSTRUCTION,
    SUMMARY_REPLY_INSTRUCTION,
    adapt_agent_prompt,
    build_command,
    build_prompt,
    default_login_check,
    load_agent_prompt,
)


def test_build_command_pins_the_wire_format():
    argv, env = build_command("PROMPT")
    assert argv[0] == "claude"
    # Every safety-critical flag is present in the pinned order.
    assert "-p" in argv and argv[argv.index("-p") + 1] == "PROMPT"
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in argv
    assert "--strict-mcp-config" in argv
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert env == {"MAX_THINKING_TOKENS": "0"}


def test_build_prompt_appends_list_instruction_for_non_summary():
    p = build_prompt("agent body", "input text")
    assert p.endswith(REPLY_INSTRUCTION)
    assert "JSON list" in p


def test_build_prompt_appends_object_instruction_for_summary():
    p = build_prompt("agent body", "input text", section="summary")
    assert p.endswith(SUMMARY_REPLY_INSTRUCTION)
    assert '"summary"' in p and '"look_first"' in p


PLUGIN_PROMPT = """---
name: pair-side-effects
tools: Read, Write
---

You write the side-effects section.

## Steps

1. Read the input file named in your prompt.
2. Use the Write tool to write the JSON list to the output path.
3. If a tool call is denied, stop and reply `denied:` followed by the tool and the reason.

## Rules

- Each item is `{"text": "...", "refs": ["<ref>"]}`.
- If nothing qualifies, write `[]`.
"""


def test_adapt_agent_prompt_drops_plugin_file_steps():
    adapted = adapt_agent_prompt(PLUGIN_PROMPT)
    assert "## Steps" not in adapted
    assert "Write tool" not in adapted
    assert "denied:" not in adapted
    assert "Each item is" in adapted
    assert "included inline below" in adapted


def test_load_agent_prompt_adapts_real_writer(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "pair-side-effects.md").write_text(PLUGIN_PROMPT)
    loaded = load_agent_prompt(agents, "side_effects")
    prompt = build_prompt(loaded, "hunk input")
    assert "Write tool" not in prompt
    assert "denied:" not in prompt
    assert prompt.endswith(REPLY_INSTRUCTION)
    assert "## Input" in prompt and "hunk input" in prompt


def test_retry_hint_shows_up_in_prompt():
    p = build_prompt("agent body", "input text", retry_hint="oops")
    assert "Previous attempt failed" in p and "oops" in p


def test_default_login_check_requires_logged_in_auth_status(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"loggedIn": true}', stderr="")

    monkeypatch.setattr("pair.service.claude.subprocess.run", run)

    assert default_login_check() is True
    assert calls[0][0] == ["claude", "auth", "status", "--json"]


def test_default_login_check_rejects_missing_or_malformed_auth(monkeypatch):
    results = iter([
        SimpleNamespace(returncode=0, stdout='{"loggedIn": false}', stderr=""),
        SimpleNamespace(returncode=0, stdout="not json", stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="not logged in"),
    ])
    monkeypatch.setattr(
        "pair.service.claude.subprocess.run",
        lambda *args, **kwargs: next(results),
    )

    assert default_login_check() is False
    assert default_login_check() is False
    assert default_login_check() is False
