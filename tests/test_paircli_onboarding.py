import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
SPEC = importlib.util.spec_from_file_location("paircli_onboarding", HOOKS_DIR / "paircli_onboarding.py")
assert SPEC and SPEC.loader
onboarding = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = onboarding
SPEC.loader.exec_module(onboarding)


def make_paircli(tmp_path: Path) -> Path:
    binary = tmp_path / "bin" / "paircli"
    binary.parent.mkdir()
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "calls = os.environ['PAIRCLI_CALLS']\n"
        "entries = json.loads(open(calls).read()) if os.path.exists(calls) else []\n"
        "entries.append(sys.argv[1:])\n"
        "json.dump(entries, open(calls, 'w'))\n"
        "if sys.argv[1:] == ['login']:\n"
        "    json.dump({'access_token': 'test-token'}, open(os.environ['PAIRCLI_AUTH_FILE'], 'w'))\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def test_start_plans_a_background_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.delenv("PAIRCLI_ONBOARDING_DISABLE", raising=False)

    plan = onboarding.start(dry_run=True)

    assert plan["status"] == "planned"
    assert plan["binary"] == str(tmp_path / "bin" / "paircli")


def test_uses_codex_plugin_data_and_active_profile(monkeypatch, tmp_path):
    plugin_data = tmp_path / "data"
    plugin_root = tmp_path / "codex" / "plugins" / "cache" / "pair-local" / "pair" / "version"
    monkeypatch.delenv("PAIRCLI_PLUGIN_HOME", raising=False)
    monkeypatch.delenv("PAIRCLI_AUTH_FILE", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("PLUGIN_DATA", str(plugin_data))
    monkeypatch.setenv("PLUGIN_ROOT", str(plugin_root))

    assert onboarding.onboarding_home() == plugin_data / "paircli"
    assert onboarding.auth_file() == plugin_data / "auth.json"
    assert onboarding.active_codex_home() == tmp_path / "codex"


def test_finds_homebrew_go_when_hook_path_is_restricted(monkeypatch, tmp_path):
    homebrew_bin = tmp_path / "homebrew-bin"
    homebrew_bin.mkdir()
    go = homebrew_bin / "go"
    go.write_text("#!/bin/sh\n")
    go.chmod(go.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(onboarding, "COMMON_TOOL_DIRECTORIES", (str(homebrew_bin),))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert onboarding.executable_path("go") == str(go)


def test_run_installs_hooks_then_logs_in(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    auth = tmp_path / "auth.json"
    calls = tmp_path / "calls.json"
    log = tmp_path / "onboarding.log"
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PAIRCLI_CALLS", str(calls))
    monkeypatch.setattr(onboarding, "ensure_paircli", lambda home, output: binary)

    result = onboarding.run(log)

    assert result == 0
    assert onboarding.read_status(tmp_path)["status"] == "complete"
    assert json.loads(calls.read_text()) == [["hook", "install"], ["login"]]


def test_run_skips_login_when_a_live_credential_exists(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    auth = tmp_path / "auth.json"
    calls = tmp_path / "calls.json"
    log = tmp_path / "onboarding.log"
    auth.write_text(json.dumps({"access_token": "already-authorized"}))
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PAIRCLI_CALLS", str(calls))
    monkeypatch.setattr(onboarding, "ensure_paircli", lambda home, output: binary)

    result = onboarding.run(log)

    assert result == 0
    assert onboarding.read_status(tmp_path)["status"] == "complete"
    assert json.loads(calls.read_text()) == [["hook", "install"]]


def test_accepts_paircli_rfc3339_expiry_on_macos_python(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "access_token": "already-authorized",
                "expires_at": "2027-01-03T15:58:58.95555+05:30",
            }
        )
    )
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))

    assert onboarding.has_active_login()
