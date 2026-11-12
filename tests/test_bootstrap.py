import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
SPEC = importlib.util.spec_from_file_location("bootstrap", HOOKS_DIR / "bootstrap.py")
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)

BINARY = b"#!/bin/sh\necho paircli\n"
DIGEST = hashlib.sha256(BINARY).hexdigest()
URL = "https://github.com/rutvikchandla3/paircli/releases/download/v0.3.0/paircli-darwin-arm64"


def manifest(**overrides):
    document = {"version": "0.3.0", "artifacts": {"darwin-arm64": {"url": URL, "sha256": DIGEST}}}
    document.update(overrides)
    return document


def fake_fetch(binary=BINARY, release=None, calls=None):
    """Stands in for curl: writes the manifest or the binary to the destination."""
    def fetch(url, dest, max_bytes, max_time=300):
        if calls is not None:
            calls.append((url, max_bytes))
        dest.write_bytes(json.dumps(release or manifest()).encode() if url == bootstrap.MANIFEST_URL else binary)
    return fetch


@pytest.mark.parametrize("system,machine,key", [
    ("Darwin", "arm64", "darwin-arm64"),
    ("Darwin", "x86_64", "darwin-amd64"),
    ("Linux", "aarch64", "linux-arm64"),
    ("Linux", "AMD64", "linux-amd64"),
])
def test_platform_key(system, machine, key):
    assert bootstrap.platform_key(system, machine) == key


@pytest.mark.parametrize("system,machine", [("Windows", "AMD64"), ("Linux", "riscv64")])
def test_platform_key_rejects_unsupported(system, machine):
    with pytest.raises(RuntimeError, match="does not support"):
        bootstrap.platform_key(system, machine)


def test_detect_harness():
    assert bootstrap.detect_harness({"PLUGIN_ROOT": "/plugins/pair"}) == "codex"
    assert bootstrap.detect_harness({"CLAUDE_PLUGIN_ROOT": "/plugins/pair"}) == "claude-code"


def test_select_artifact_picks_the_platform():
    other = {"url": "https://example.com/other", "sha256": "0" * 64}
    release = manifest(artifacts={"darwin-arm64": {"url": URL, "sha256": DIGEST.upper()}, "linux-amd64": other})
    assert bootstrap.select_artifact(release, "darwin-arm64") == (URL, DIGEST)
    with pytest.raises(RuntimeError, match="does not support linux-arm64"):
        bootstrap.select_artifact(release, "linux-arm64")


def test_select_artifact_rejects_http_and_bad_digest():
    http = manifest(artifacts={"darwin-arm64": {"url": URL.replace("https", "http"), "sha256": DIGEST}})
    with pytest.raises(RuntimeError, match="https"):
        bootstrap.select_artifact(http, "darwin-arm64")
    short = manifest(artifacts={"darwin-arm64": {"url": URL, "sha256": "abc"}})
    with pytest.raises(RuntimeError, match="invalid sha256"):
        bootstrap.select_artifact(short, "darwin-arm64")


def test_select_artifact_checks_min_plugin_version():
    assert bootstrap.select_artifact(manifest(min_plugin_version="0.3.0"), "darwin-arm64")[0] == URL
    with pytest.raises(RuntimeError, match="requires Pair plugin 9.0.0"):
        bootstrap.select_artifact(manifest(min_plugin_version="9.0.0"), "darwin-arm64")


def test_install_binary_is_atomic_and_executable(tmp_path):
    target = tmp_path / ".paircli" / "bin" / "paircli"
    calls = []
    bootstrap.install_binary(target, "darwin-arm64", fetch=fake_fetch(calls=calls))
    assert target.read_bytes() == BINARY
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert [name for name in os.listdir(target.parent)] == ["paircli"]
    assert calls == [(bootstrap.MANIFEST_URL, 1024 * 1024), (URL, bootstrap.MAX_BINARY_BYTES)]


def test_install_binary_rejects_sha256_mismatch(tmp_path):
    target = tmp_path / "bin" / "paircli"
    with pytest.raises(RuntimeError, match="sha256"):
        bootstrap.install_binary(target, "darwin-arm64", fetch=fake_fetch(binary=b"tampered"))
    assert not target.exists() and os.listdir(target.parent) == []


def test_install_binary_keeps_existing_binary_on_failure(tmp_path):
    target = tmp_path / "bin" / "paircli"
    target.parent.mkdir()
    target.write_bytes(b"old")
    with pytest.raises(RuntimeError):
        bootstrap.install_binary(target, "darwin-arm64", fetch=fake_fetch(binary=b"tampered"))
    assert target.read_bytes() == b"old"


def test_install_binary_rejects_http_url(tmp_path):
    release = manifest(artifacts={"darwin-arm64": {"url": "http://example.com/paircli", "sha256": DIGEST}})
    with pytest.raises(RuntimeError, match="https"):
        bootstrap.install_binary(tmp_path / "bin" / "paircli", "darwin-arm64", fetch=fake_fetch(release=release))


def test_install_binary_rejects_oversized_download(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "MAX_BINARY_BYTES", 4)
    with pytest.raises(RuntimeError, match="size limit"):
        bootstrap.install_binary(tmp_path / "bin" / "paircli", "darwin-arm64", fetch=fake_fetch())


def test_hook_output_shape():
    out = json.loads(bootstrap.hook_output("Pair setup started"))
    assert out == {"systemMessage": "Pair setup started",
                   "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "Pair setup started"}}


def fake_cli(path, status):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\ncat <<'EOF'\n" + (status if isinstance(status, str) else json.dumps(status)) + "\nEOF\n")
    path.chmod(0o755)
    return path


def test_status_line_before_install_says_what_happens_next(tmp_path):
    line = bootstrap.status_line(tmp_path / "paircli", "codex")
    assert line.startswith("Pair setup started: installing the Pair CLI")
    assert "approve the login in your browser" in line and "~/.paircli/logs/setup-*.log" in line


def test_status_line_from_status_json(tmp_path):
    status = {"cli_version": "0.3.0", "logged_in": True, "hooks": {"codex": True, "claude-code": False}}
    binary = fake_cli(tmp_path / "paircli", status)
    assert bootstrap.status_line(binary, "codex") == "Pair CLI 0.3.0: logged in, hook installed"
    assert bootstrap.status_line(binary, "claude-code") == "Pair CLI 0.3.0: logged in, hook pending"


def test_status_line_while_login_is_pending_says_to_approve_in_the_browser(tmp_path):
    status = {"cli_version": "0.3.0", "logged_in": False, "hooks": {"codex": True}}
    line = bootstrap.status_line(fake_cli(tmp_path / "paircli", status), "codex")
    assert line.startswith("Pair CLI 0.3.0: login pending, hook installed. ")
    assert "approve the login in your browser" in line
    assert line.endswith("device code and URL are in ~/.paircli/logs/setup-*.log")


def test_status_line_names_the_setup_log_the_cli_reports(tmp_path):
    log = "/home/me/.paircli/logs/setup-20261027-093000.log"
    status = {"logged_in": False, "hooks": {}, "setup": {"state": "running", "log": log}}
    assert bootstrap.status_line(fake_cli(tmp_path / "paircli", status), "codex").endswith(f"are in {log}")


def test_status_line_when_status_is_unreadable_still_says_what_to_do(tmp_path):
    line = bootstrap.status_line(fake_cli(tmp_path / "paircli", "not json"), "codex")
    assert line.startswith("Pair setup started; if it asks for a login, approve the login in your browser")
    assert "~/.paircli/logs/setup-*.log" in line


def test_preflight_reports_missing_tools_in_one_line_each():
    have = lambda *names: lambda name: f"/usr/bin/{name}" if name in names else None
    assert bootstrap.preflight(False, have("curl", "gh")) == (None, None)
    blocker, note = bootstrap.preflight(False, have())
    assert blocker.startswith("curl not found") and "\n" not in blocker
    assert note.startswith("gh not found") and "gh auth login" in note and "\n" not in note
    assert bootstrap.preflight(False, have("curl")) == (None, note)


def test_preflight_does_not_need_curl_once_the_cli_is_installed():
    assert bootstrap.preflight(True, lambda name: None)[0] is None


def only_tools(monkeypatch, *names):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: f"/usr/bin/{name}" if name in names else None)


def test_main_prints_one_json_object_and_starts_detached_child(tmp_path, monkeypatch, capsys):
    only_tools(monkeypatch, "curl", "gh")
    started = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            started.append((command, kwargs))

    monkeypatch.setattr(bootstrap.subprocess, "Popen", FakePopen)
    monkeypatch.delenv("PLUGIN_ROOT", raising=False)
    assert bootstrap.main([], home=tmp_path) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["systemMessage"].startswith("Pair setup started: installing")
    [(command, kwargs)] = started
    assert command[-2:] == ["run", "claude-code"] and kwargs["start_new_session"] is True
    [log] = (tmp_path / ".paircli" / "logs").glob("bootstrap-*.log")
    assert log.exists()


def test_main_without_curl_says_why_and_starts_nothing(tmp_path, monkeypatch, capsys):
    only_tools(monkeypatch, "gh")
    monkeypatch.setattr(bootstrap, "start_child", lambda *a: pytest.fail("started a child that cannot install"))
    assert bootstrap.main([], home=tmp_path) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["systemMessage"].startswith("Pair setup did not start: curl not found")


def test_main_without_gh_adds_a_note_but_still_sets_up(tmp_path, monkeypatch, capsys):
    only_tools(monkeypatch, "curl")
    started = []
    monkeypatch.setattr(bootstrap, "start_child", lambda *a: started.append(a))
    assert bootstrap.main([], home=tmp_path) == 0
    message = json.loads(capsys.readouterr().out)["systemMessage"]
    assert message.startswith("Pair setup started") and ". Note: gh not found; install the GitHub CLI" in message
    assert len(started) == 1 and "\n" not in message


def test_main_with_the_cli_installed_does_not_need_curl(tmp_path, monkeypatch, capsys):
    only_tools(monkeypatch, "gh")
    fake_cli(tmp_path / ".paircli" / "bin" / "paircli", {"cli_version": "0.3.0", "logged_in": True, "hooks": {"claude-code": True}})
    monkeypatch.delenv("PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(bootstrap, "start_child", lambda *a: None)
    assert bootstrap.main([], home=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["systemMessage"] == "Pair CLI 0.3.0: logged in, hook installed"


def test_main_never_raises(tmp_path, monkeypatch, capsys):
    only_tools(monkeypatch, "curl", "gh")
    def boom(*args, **kwargs):
        raise OSError("no fork")

    monkeypatch.setattr(bootstrap.subprocess, "Popen", boom)
    assert bootstrap.main([], home=tmp_path) == 0
    assert "could not start: no fork" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_child_failure_is_logged_not_raised(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "platform_key", lambda: (_ for _ in ()).throw(RuntimeError("unsupported")))
    assert bootstrap.main(["run", "codex"], home=tmp_path) == 0
    assert "pair bootstrap failed: unsupported" in capsys.readouterr().out


def test_child_runs_setup_with_harness_and_plugin_version(tmp_path, monkeypatch):
    binary = tmp_path / ".paircli" / "bin" / "paircli"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    seen = []

    class Done:
        returncode = 0

    monkeypatch.setattr(bootstrap.subprocess, "run", lambda command, **kwargs: seen.append(command) or Done())
    bootstrap.run_child("codex", tmp_path)
    assert seen == [[str(binary), "setup", "--harness", "codex", "--plugin-version", bootstrap.PLUGIN_VERSION]]


ROOT = HOOKS_DIR.parent


def test_plugin_versions_agree():
    claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())["version"]
    pyproject = next(line for line in (ROOT / "pyproject.toml").read_text().splitlines() if line.startswith("version"))
    assert claude == codex == bootstrap.PLUGIN_VERSION
    assert pyproject.split('"')[1] == claude


def test_hook_command_uses_only_claude_plugin_root():
    # The Claude directory validator rejects any other variable in a hook command; Codex sets this one too.
    command = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/bootstrap.py"'


def test_manifest_icons_exist_and_are_square_png():
    claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())["interface"]
    for ref in {claude["icon"], codex["logo"], codex["composerIcon"]}:
        assert ref.startswith("./")
        data = (ROOT / ref).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        assert width == height and 512 <= width <= 2048
        assert len(data) < 2 * 1024 * 1024
