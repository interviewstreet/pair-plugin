import importlib.util
import json
import os
import stat
import sys
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest

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


def test_platform_key_normalizes_supported_macos_arm64(monkeypatch):
    monkeypatch.setattr(onboarding.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(onboarding.platform, "machine", lambda: "aarch64")

    assert onboarding.platform_key() == "darwin-arm64"


def test_ensure_paircli_downloads_and_verifies_release(monkeypatch, tmp_path):
    binary_contents = b"#!/bin/sh\necho paircli\n"
    checksum = sha256(binary_contents).hexdigest()
    manifest = json.dumps(
        {
            "version": "0.1.0",
            "artifacts": {"darwin-arm64": {"url": "https://cdn.example.test/paircli", "sha256": checksum}},
        }
    ).encode()
    responses = iter((BytesIO(manifest), BytesIO(binary_contents)))

    monkeypatch.setenv("PAIRCLI_RELEASE_MANIFEST_URL", "https://cdn.example.test/manifest.json")
    monkeypatch.delenv("PAIRCLI_DEV_BUILD", raising=False)
    monkeypatch.setattr(onboarding, "platform_key", lambda: "darwin-arm64")
    monkeypatch.setattr(onboarding, "urlopen", lambda *_args, **_kwargs: next(responses))

    binary = onboarding.ensure_paircli(tmp_path, None)

    assert binary.read_bytes() == binary_contents
    assert binary.stat().st_mode & stat.S_IXUSR
    assert json.loads((tmp_path / "release.json").read_text()) == {
        "version": "0.1.0",
        "platform": "darwin-arm64",
        "sha256": checksum,
    }


def test_ensure_paircli_rejects_checksum_mismatch(monkeypatch, tmp_path):
    manifest = json.dumps(
        {
            "version": "0.1.0",
            "artifacts": {"darwin-arm64": {"url": "https://cdn.example.test/paircli", "sha256": "0" * 64}},
        }
    ).encode()
    responses = iter((BytesIO(manifest), BytesIO(b"unexpected-binary")))

    monkeypatch.setenv("PAIRCLI_RELEASE_MANIFEST_URL", "https://cdn.example.test/manifest.json")
    monkeypatch.delenv("PAIRCLI_DEV_BUILD", raising=False)
    monkeypatch.setattr(onboarding, "platform_key", lambda: "darwin-arm64")
    monkeypatch.setattr(onboarding, "urlopen", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(RuntimeError, match="checksum"):
        onboarding.ensure_paircli(tmp_path, None)
    assert not (tmp_path / "bin" / "paircli").exists()


def test_ensure_paircli_uses_public_manifest_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PAIRCLI_RELEASE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("PAIRCLI_DEV_BUILD", raising=False)
    binary_contents = b"paircli"
    checksum = sha256(binary_contents).hexdigest()
    manifest = json.dumps(
        {
            "version": "0.1.0",
            "artifacts": {"darwin-arm64": {"url": "https://cdn.example.test/paircli", "sha256": checksum}},
        }
    ).encode()
    responses = iter((BytesIO(manifest), BytesIO(binary_contents)))
    requested_urls = []

    monkeypatch.setattr(onboarding, "platform_key", lambda: "darwin-arm64")
    monkeypatch.setattr(
        onboarding,
        "urlopen",
        lambda request, **_kwargs: (requested_urls.append(request.full_url), next(responses))[1],
    )

    onboarding.ensure_paircli(tmp_path, None)

    assert requested_urls[0] == onboarding.DEFAULT_RELEASE_MANIFEST_URL


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
    assert json.loads(calls.read_text()) == [["update", "--quiet"], ["hook", "install"], ["login"]]


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
    assert json.loads(calls.read_text()) == [["update", "--quiet"], ["hook", "install"]]


def test_run_continues_when_update_is_unavailable(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    auth = tmp_path / "auth.json"
    calls = tmp_path / "calls.json"
    log = tmp_path / "onboarding.log"
    original_run_command = onboarding.run_command
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PAIRCLI_CALLS", str(calls))
    monkeypatch.setattr(onboarding, "ensure_paircli", lambda home, output: binary)

    def failing_update(command, *, cwd, log):
        if command[1:] == ["update", "--quiet"]:
            raise RuntimeError("command exited 1: paircli update")
        original_run_command(command, cwd=cwd, log=log)

    monkeypatch.setattr(onboarding, "run_command", failing_update)

    assert onboarding.run(log) == 0
    assert onboarding.read_status(tmp_path)["status"] == "complete"
    assert json.loads(calls.read_text()) == [["hook", "install"], ["login"]]
    assert "paircli update skipped" in log.read_text()


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
