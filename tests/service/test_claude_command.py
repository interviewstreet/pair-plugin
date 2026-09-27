"""build_command argv + reply instructions."""

from pair.service.claude import (
    REPLY_INSTRUCTION,
    SUMMARY_REPLY_INSTRUCTION,
    build_command,
    build_prompt,
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


def test_retry_hint_shows_up_in_prompt():
    p = build_prompt("agent body", "input text", retry_hint="oops")
    assert "Previous attempt failed" in p and "oops" in p
