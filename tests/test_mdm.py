"""The MDM scripts and the managed-hook templates in mdm/."""
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_URL = "https://github.com/interviewstreet/pair-plugin/releases/latest/download/install.sh"


def cli_events(package):
    """The events the CLI installs hooks for, read from its hookEvents table."""
    source = (ROOT / f"cli/internal/{package}/hooks.go").read_text()
    table = source[source.index("var hookEvents = []hookEventSpec{"):]
    table = table[:table.index("\n}\n")]
    return dict(re.findall(r'\{Event: "(\w+)"(?:, Matcher: "(\w+)")?\}', table))


@pytest.mark.parametrize("script", ["mdm/macos/install-pair.sh", "mdm/linux/install-pair.sh"])
def test_mdm_scripts_run_the_release_installer_without_login(script):
    path = ROOT / script
    subprocess.run(["sh", "-n", str(path)], check=True)
    text = path.read_text()
    assert f'installer_url="{INSTALLER_URL}"' in text
    assert "--proto =https" in text
    assert re.search(r'sh "\$script" --no-login </dev/null', text)
    assert path.stat().st_mode & 0o111


def test_mdm_scripts_install_the_wrapper_the_templates_call():
    wrapper = (ROOT / "mdm/templates/pair-hook.sh").read_text()
    for script in ("mdm/macos/install-pair.sh", "mdm/linux/install-pair.sh"):
        text = (ROOT / script).read_text()
        embedded = text[text.index("<<'WRAPPER'\n") + len("<<'WRAPPER'\n"):text.index("WRAPPER\nchmod")]
        assert 'mv -f "$managed_dir/.pair-hook.sh.$$" "$managed_dir/pair-hook.sh"' in text
        assert 'chmod 755 "$managed_dir"\n' in text
        assert embedded == wrapper, script
        assert "managed_dir=/usr/local/share/pair" in text


def test_wrapper_runs_the_users_cli_and_is_quiet_without_it(tmp_path):
    wrapper = ROOT / "mdm/templates/pair-hook.sh"
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    assert subprocess.run(["sh", str(wrapper), "hook", "codex", "Stop"], env=env).returncode == 0
    cli = tmp_path / ".paircli/bin/paircli"
    cli.parent.mkdir(parents=True)
    cli.write_text('#!/bin/sh\necho "ran $*"\n')
    cli.chmod(0o755)
    out = subprocess.run(["sh", str(wrapper), "hook", "codex", "Stop"], env=env, capture_output=True, text=True)
    assert out.stdout == "ran hook codex Stop\n"


def test_linux_script_needs_a_user():
    result = subprocess.run(["sh", str(ROOT / "mdm/linux/install-pair.sh")], capture_output=True, text=True)
    assert result.returncode == 2 and "usage:" in result.stderr


def test_claude_managed_settings_template_matches_the_cli_hooks():
    settings = json.loads((ROOT / "mdm/templates/claude-managed-settings.json").read_text())
    assert settings["allowManagedHooksOnly"] is True
    events = cli_events("claudecode")
    assert set(settings["hooks"]) == set(events)
    for event, matcher in events.items():
        [group] = settings["hooks"][event]
        assert group.get("matcher", "") == matcher
        [hook] = group["hooks"]
        assert hook == {"type": "command", "command": f"/usr/local/share/pair/pair-hook.sh hook claude-code {event}", "timeout": 10}


def test_codex_requirements_template_matches_the_cli_hooks():
    requirements = tomllib.loads((ROOT / "mdm/templates/codex-requirements.toml").read_text())
    assert requirements["allow_managed_hooks_only"] is True and requirements["features"]["hooks"] is True
    assert requirements["hooks"].pop("managed_dir") == "/usr/local/share/pair"
    events = cli_events("codex")
    assert set(requirements["hooks"]) == set(events)
    for event, matcher in events.items():
        [group] = requirements["hooks"][event]
        assert group.get("matcher", "") == matcher
        [hook] = group["hooks"]
        assert hook["type"] == "command"
        assert hook["command"] == f"/usr/local/share/pair/pair-hook.sh hook codex {event}"
        # The CLI writes 10 everywhere; Codex clamps Interrupt and SessionEnd to 3.
        assert hook["timeout"] == (3 if event in ("Interrupt", "SessionEnd") else 10)
