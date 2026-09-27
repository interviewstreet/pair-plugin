"""build_command argv + reply instructions."""

from types import SimpleNamespace

from pair.service.claude import (
    REPLY_INSTRUCTION,
    SUMMARY_REPLY_INSTRUCTION,
    build_command,
    build_prompt,
    default_login_check,
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
