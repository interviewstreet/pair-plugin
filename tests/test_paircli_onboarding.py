import importlib.util
import json
import os
import stat
import sys
from email.message import Message
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))
SPEC = importlib.util.spec_from_file_location("paircli_onboarding", HOOKS_DIR / "paircli_onboarding.py")
assert SPEC and SPEC.loader
onboarding = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = onboarding
SPEC.loader.exec_module(onboarding)


class FakeResponse(BytesIO):
    def __init__(self, payload: bytes, url: str, content_type: str = "application/json"):
        super().__init__(payload)
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def geturl(self) -> str:
        return self.url


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
        "    server = os.environ.get('PAIRCLI_SERVER_URL') or 'https://prod-be.pair.sh'\n"
        "    json.dump({'access_token': 'test-token', 'server_url': server.rstrip('/')}, "
        "open(os.environ['PAIRCLI_AUTH_FILE'], 'w'))\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def test_start_plans_a_background_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.delenv("PAIRCLI_ONBOARDING_DISABLE", raising=False)

    plan = onboarding.start("codex", dry_run=True)

    assert plan["status"] == "planned"
    assert plan["binary"] == str(tmp_path / "bin" / "paircli")


def test_start_automatically_bootstraps_paircli(monkeypatch, tmp_path):
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(onboarding.subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)))

    result = onboarding.start("codex")

    assert result["status"] == "started"
    assert onboarding.has_consent(tmp_path, "codex")
    assert calls[0][0][2:] == ["run", "--log", result["log"], "--harness", "codex"]
    assert not (tmp_path / "bin" / "paircli").exists()


def test_start_skips_completed_active_onboarding(monkeypatch, tmp_path):
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    binary = onboarding.binary_path(tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_text("paircli")
    binary.chmod(0o755)
    onboarding.grant_consent(tmp_path, "codex")
    onboarding.write_status(tmp_path, "complete", binary=str(binary), log="/tmp/onboarding.log")
    monkeypatch.setattr(onboarding, "has_active_login", lambda: True)
    monkeypatch.setattr(onboarding.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("unexpected onboarding restart"))

    result = onboarding.start("codex")

    assert result == {"status": "complete", "binary": str(binary), "log": "/tmp/onboarding.log"}


def test_start_restarts_onboarding_for_a_different_backend(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "local-token", "server_url": "http://localhost:8080"}))
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PAIRCLI_SERVER_URL", "https://private.pair.example")
    onboarding.grant_consent(tmp_path, "codex")
    onboarding.write_status(tmp_path, "complete", binary=str(binary), log="/tmp/old.log")
    launched = []
    monkeypatch.setattr(onboarding.subprocess, "Popen", lambda command, **kwargs: launched.append(command))

    result = onboarding.start("codex")

    assert result["status"] == "started"
    assert launched[0][2] == "run"


def test_start_schedules_scan_for_completed_onboarding(monkeypatch, tmp_path):
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    binary = make_paircli(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    onboarding.grant_consent(tmp_path, "codex")
    onboarding.write_status(tmp_path, "complete", binary=str(binary), log="/tmp/onboarding.log")
    monkeypatch.setattr(onboarding, "has_active_login", lambda: True)
    calls = []
    monkeypatch.setattr(onboarding.subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs)))

    result = onboarding.start("codex", pr_url="https://github.com/acme/repo/pull/42", repo_dir=str(repo))

    assert result["status"] == "complete"
    assert result["scan"]["status"] == "started"
    assert calls[0][0][2] == "scan"
    assert "--pr-url" in calls[0][0] and "https://github.com/acme/repo/pull/42" in calls[0][0]


def test_run_schedules_pending_scan_after_login(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    auth = tmp_path / "auth.json"
    calls = tmp_path / "calls.json"
    log = tmp_path / "onboarding.log"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PAIRCLI_CALLS", str(calls))
    monkeypatch.setattr(onboarding, "ensure_paircli", lambda home, output: binary)
    scheduled = []
    monkeypatch.setattr(onboarding, "launch_pending_scan", lambda home, executable, harness: scheduled.append((home, executable, harness)))
    onboarding.grant_consent(tmp_path, "codex")
    onboarding.queue_scan(tmp_path, "https://github.com/acme/repo/pull/42", str(repo))

    assert onboarding.run(log, "codex") == 0
    assert scheduled == [(tmp_path, binary, "codex")]


def test_scan_checks_for_an_update_before_generating_artifacts(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    calls = tmp_path / "calls.json"
    log = tmp_path / "scan.log"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_CALLS", str(calls))
    monkeypatch.setenv("PAIRCLI_UPLOAD_ENABLED", "0")
    artifacts = repo / ".paircli" / "pr-42" / ("a" * 40)
    artifacts.mkdir(parents=True)
    monkeypatch.setattr(onboarding, "artifact_directory", lambda *_: artifacts)
    generated = []
    def write_summary(*args):
        generated.append(args)
        summary = artifacts / "summary.md"
        summary.write_text("# PR summary\n")
        return summary

    monkeypatch.setattr(onboarding, "write_summary", write_summary)

    assert onboarding.scan(log, "https://github.com/acme/repo/pull/42", repo, "codex") == 0
    assert json.loads(calls.read_text()) == [
        ["update", "--quiet", "--plugin-version", "0.1.0"],
        ["scan", "https://github.com/acme/repo/pull/42", "--snapshot", "--out", str(artifacts)],
    ]
    assert generated[0][:4] == ("codex", "https://github.com/acme/repo/pull/42", repo, artifacts)
    status = json.loads(onboarding.scan_status_path(tmp_path).read_text())
    assert status["summary"] == str(artifacts / "summary.md")
    assert status["archive"] == str(artifacts.with_suffix(".zip"))
    assert status["artifact_id"] == ""


def test_scan_uploads_archive_by_default(monkeypatch, tmp_path):
    log = tmp_path / "scan.log"
    repo = tmp_path / "repo"
    artifacts = repo / ".paircli" / "pr-42" / ("a" * 40)
    artifacts.mkdir(parents=True)
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.delenv("PAIRCLI_UPLOAD_ENABLED", raising=False)
    monkeypatch.setattr(onboarding, "artifact_directory", lambda *_: artifacts)
    monkeypatch.setattr(onboarding, "run_command", lambda *args, **kwargs: None)

    def write_summary(*_args):
        summary = artifacts / "summary.md"
        summary.write_text("# PR summary\n")
        return summary

    monkeypatch.setattr(onboarding, "write_summary", write_summary)
    uploads = []
    notifications = []
    monkeypatch.setattr(onboarding, "upload_archive", lambda *args: uploads.append(args) or "artifact-123")
    monkeypatch.setattr(onboarding, "notify_scan_result", lambda *args, **kwargs: notifications.append((args, kwargs)))

    assert onboarding.scan(log, "https://github.com/acme/repo/pull/42", repo, "codex") == 0
    assert uploads == [("https://github.com/acme/repo/pull/42", artifacts.with_suffix(".zip"), onboarding.auth_file())]
    status = json.loads(onboarding.scan_status_path(tmp_path).read_text())
    assert status["artifact_id"] == "artifact-123"
    assert notifications == [
        (("https://github.com/acme/repo/pull/42",), {"succeeded": True, "uploaded": True})
    ]


def test_scan_keeps_zip_when_backend_rejects_cli_login(monkeypatch, tmp_path):
    log = tmp_path / "scan.log"
    repo = tmp_path / "repo"
    artifacts = repo / ".paircli" / "pr-42" / ("a" * 40)
    artifacts.mkdir(parents=True)
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_UPLOAD_ENABLED", "1")
    monkeypatch.setattr(onboarding, "artifact_directory", lambda *_: artifacts)
    monkeypatch.setattr(onboarding, "run_command", lambda *args, **kwargs: None)

    def write_summary(*_args):
        summary = artifacts / "summary.md"
        summary.write_text("# PR summary\n")
        return summary

    def reject_upload(*_args):
        raise RuntimeError("Pair BE does not accept this CLI login")

    monkeypatch.setattr(onboarding, "write_summary", write_summary)
    monkeypatch.setattr(onboarding, "upload_archive", reject_upload)
    notifications = []
    monkeypatch.setattr(onboarding, "notify_scan_result", lambda *args, **kwargs: notifications.append((args, kwargs)))

    assert onboarding.scan(log, "https://github.com/acme/repo/pull/42", repo, "codex") == 1
    status = json.loads(onboarding.scan_status_path(tmp_path).read_text())
    assert status["status"] == "failed"
    assert status["summary"] == str(artifacts / "summary.md")
    assert status["archive"] == str(artifacts.with_suffix(".zip"))
    assert artifacts.with_suffix(".zip").is_file()
    assert notifications == [
        (("https://github.com/acme/repo/pull/42",), {"succeeded": False})
    ]


def test_scan_records_summary_failure_and_removes_stale_cli_summary(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    artifacts = repo / ".paircli" / "pr-42" / ("b" * 40)
    artifacts.mkdir(parents=True)
    (artifacts / "summary.md").write_text("old CLI summary")
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_CALLS", str(tmp_path / "calls.json"))
    monkeypatch.setattr(onboarding, "artifact_directory", lambda *_: artifacts)

    def fail_summary(*_args):
        raise RuntimeError("agent unavailable")

    monkeypatch.setattr(onboarding, "write_summary", fail_summary)

    assert onboarding.scan(tmp_path / "scan.log", "https://github.com/acme/repo/pull/42", repo, "codex") == 1
    assert not (artifacts / "summary.md").exists()
    status = json.loads(onboarding.scan_status_path(tmp_path).read_text())
    assert status["status"] == "failed"
    assert status["error"] == "agent unavailable"
    assert status["artifacts"] == str(artifacts)


def test_artifact_directory_uses_pr_head_commit(monkeypatch, tmp_path):
    from types import SimpleNamespace

    commit = "c" * 40
    replies = iter((str(tmp_path) + "\n", commit + "\n"))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=next(replies), stderr="")

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)

    result = onboarding.artifact_directory("https://github.com/acme/repo/pull/42", tmp_path)

    assert result == tmp_path / ".paircli" / "pr-42" / commit
    assert calls[1][-2:] == ["--jq", ".commits[-1].oid"]


def test_stale_lock_is_reclaimed(monkeypatch, tmp_path):
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    tmp_path.mkdir(exist_ok=True)
    onboarding.lock_path(tmp_path).write_text('{"pid": -1, "created_at": "2000-01-01T00:00:00+00:00"}')

    assert onboarding.acquire_lock(tmp_path)
    onboarding.release_lock(tmp_path)


def test_approval_retries_a_previous_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    onboarding.write_status(tmp_path, "failed", error="network unavailable")
    monkeypatch.setattr(onboarding, "start", lambda harness: {"status": "started", "harness": harness})

    assert onboarding.approve("codex") == {"status": "started", "harness": "codex"}
    assert onboarding.read_status(tmp_path) == {}


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
    responses = iter((manifest, binary_contents))

    monkeypatch.setenv("PAIRCLI_RELEASE_MANIFEST_URL", "https://cdn.example.test/manifest.json")
    monkeypatch.delenv("PAIRCLI_DEV_BUILD", raising=False)
    monkeypatch.setattr(onboarding, "platform_key", lambda: "darwin-arm64")
    monkeypatch.setattr(onboarding, "urlopen", lambda request, **_kwargs: FakeResponse(next(responses), request.full_url))

    binary = onboarding.ensure_paircli(tmp_path, None)

    assert binary.read_bytes() == binary_contents
    assert binary.stat().st_mode & stat.S_IXUSR
    assert json.loads((tmp_path / "release.json").read_text()) == {
        "version": "0.1.0",
        "platform": "darwin-arm64",
        "sha256": checksum,
    }


def test_fetch_json_retries_transport_timeouts(monkeypatch):
    manifest = json.dumps({"version": "0.1.0", "artifacts": {}}).encode()
    attempts = []
    delays = []

    def flaky_urlopen(request, **kwargs):
        attempts.append((request.full_url, kwargs["timeout"]))
        if len(attempts) < 3:
            raise TimeoutError("read timed out")
        return FakeResponse(manifest, request.full_url)

    monkeypatch.setattr(onboarding, "urlopen", flaky_urlopen)
    monkeypatch.setattr(onboarding.time, "sleep", delays.append)

    assert onboarding.fetch_json("https://cdn.example.test/latest.json")["version"] == "0.1.0"
    assert attempts == [
        ("https://cdn.example.test/latest.json", onboarding.MANIFEST_TIMEOUT_SECONDS),
        ("https://cdn.example.test/latest.json", onboarding.MANIFEST_TIMEOUT_SECONDS),
        ("https://cdn.example.test/latest.json", onboarding.MANIFEST_TIMEOUT_SECONDS),
    ]
    assert delays == list(onboarding.RELEASE_RETRY_DELAYS_SECONDS)


def test_download_binary_retries_transport_timeout(monkeypatch, tmp_path):
    binary_contents = b"paircli"
    checksum = sha256(binary_contents).hexdigest()
    attempts = []
    delays = []

    def flaky_urlopen(request, **kwargs):
        attempts.append(kwargs["timeout"])
        if len(attempts) == 1:
            raise TimeoutError("read timed out")
        return FakeResponse(binary_contents, request.full_url, content_type="application/octet-stream")

    monkeypatch.setattr(onboarding, "urlopen", flaky_urlopen)
    monkeypatch.setattr(onboarding.time, "sleep", delays.append)

    destination = tmp_path / "bin" / "paircli"
    onboarding.download_binary("https://cdn.example.test/paircli", destination, checksum)

    assert destination.read_bytes() == binary_contents
    assert attempts == [onboarding.BINARY_TIMEOUT_SECONDS, onboarding.BINARY_TIMEOUT_SECONDS]
    assert delays == [onboarding.RELEASE_RETRY_DELAYS_SECONDS[0]]


def test_ensure_paircli_rejects_checksum_mismatch(monkeypatch, tmp_path):
    manifest = json.dumps(
        {
            "version": "0.1.0",
            "artifacts": {"darwin-arm64": {"url": "https://cdn.example.test/paircli", "sha256": "0" * 64}},
        }
    ).encode()
    responses = iter((manifest, b"unexpected-binary"))

    monkeypatch.setenv("PAIRCLI_RELEASE_MANIFEST_URL", "https://cdn.example.test/manifest.json")
    monkeypatch.delenv("PAIRCLI_DEV_BUILD", raising=False)
    monkeypatch.setattr(onboarding, "platform_key", lambda: "darwin-arm64")
    monkeypatch.setattr(onboarding, "urlopen", lambda request, **_kwargs: FakeResponse(next(responses), request.full_url))

    with pytest.raises(RuntimeError, match="checksum"):
        onboarding.ensure_paircli(tmp_path, None)
    assert not (tmp_path / "bin" / "paircli").exists()


def test_ensure_paircli_rejects_manifest_redirect_or_html(monkeypatch, tmp_path):
    monkeypatch.setenv("PAIRCLI_RELEASE_MANIFEST_URL", "https://cdn.example.test/manifest.json")
    monkeypatch.delenv("PAIRCLI_DEV_BUILD", raising=False)
    monkeypatch.setattr(onboarding, "urlopen", lambda request, **_kwargs: FakeResponse(b"<html>", "https://www.example.test/"))

    with pytest.raises(RuntimeError, match="different origin"):
        onboarding.ensure_paircli(tmp_path, None)

    monkeypatch.setattr(
        onboarding,
        "urlopen",
        lambda request, **_kwargs: FakeResponse(b"<html>", request.full_url, content_type="text/html"),
    )
    with pytest.raises(RuntimeError, match="application/json"):
        onboarding.ensure_paircli(tmp_path, None)


def test_allows_github_release_asset_redirect():
    response = FakeResponse(
        b"{}",
        "https://release-assets.githubusercontent.com/github-production-release-asset/example",
    )

    onboarding.verify_response_origin(
        response,
        "https://github.com/rutvikchandla3/paircli/releases/latest/download/latest.json",
        label="paircli release manifest",
    )


def test_accepts_octet_stream_only_for_github_release_assets():
    response = FakeResponse(
        b"{}",
        "https://release-assets.githubusercontent.com/github-production-release-asset/example",
        content_type="application/octet-stream",
    )

    onboarding.verify_json_response(
        response,
        "https://github.com/rutvikchandla3/paircli/releases/latest/download/latest.json",
    )

    with pytest.raises(RuntimeError, match="application/json"):
        onboarding.verify_json_response(
            FakeResponse(b"{}", "https://mirror.example.test/latest.json", content_type="application/octet-stream"),
            "https://mirror.example.test/latest.json",
        )


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
    responses = iter((manifest, binary_contents))
    requested_urls = []

    monkeypatch.setattr(onboarding, "platform_key", lambda: "darwin-arm64")
    monkeypatch.setattr(
        onboarding,
        "urlopen",
        lambda request, **_kwargs: (
            requested_urls.append(request.full_url), FakeResponse(next(responses), request.full_url)
        )[1],
    )

    onboarding.ensure_paircli(tmp_path, None)

    assert requested_urls[0] == onboarding.DEFAULT_RELEASE_MANIFEST_URL
    assert requested_urls[0] == "https://github.com/rutvikchandla3/paircli/releases/latest/download/latest.json"


def test_ensure_paircli_rejects_an_incompatible_plugin(monkeypatch, tmp_path):
    manifest = json.dumps(
        {
            "version": "0.1.0",
            "min_plugin_version": "99.0.0",
            "artifacts": {"darwin-arm64": {"url": "https://cdn.example.test/paircli", "sha256": "0" * 64}},
        }
    ).encode()
    monkeypatch.setenv("PAIRCLI_RELEASE_MANIFEST_URL", "https://cdn.example.test/manifest.json")
    monkeypatch.setattr(onboarding, "urlopen", lambda request, **_kwargs: FakeResponse(manifest, request.full_url))

    with pytest.raises(RuntimeError, match="requires Pair plugin 99.0.0"):
        onboarding.ensure_paircli(tmp_path, None)


def test_run_installs_hooks_then_logs_in(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    auth = tmp_path / "auth.json"
    calls = tmp_path / "calls.json"
    log = tmp_path / "onboarding.log"
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PAIRCLI_CALLS", str(calls))
    monkeypatch.delenv("PAIRCLI_SERVER_URL", raising=False)
    monkeypatch.setattr(onboarding, "ensure_paircli", lambda home, output: binary)
    onboarding.grant_consent(tmp_path, "codex")

    result = onboarding.run(log, "codex")

    assert result == 0
    assert onboarding.read_status(tmp_path)["status"] == "complete"
    assert json.loads(calls.read_text()) == [["update", "--quiet", "--plugin-version", "0.1.0"], ["hook", "install", "codex"], ["login"]]
    assert json.loads(auth.read_text())["server_url"] == onboarding.DEFAULT_SERVER_URL


def test_run_skips_login_when_a_live_credential_exists(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    auth = tmp_path / "auth.json"
    calls = tmp_path / "calls.json"
    log = tmp_path / "onboarding.log"
    auth.write_text(json.dumps({"access_token": "already-authorized", "server_url": onboarding.DEFAULT_SERVER_URL}))
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PAIRCLI_SERVER_URL", onboarding.DEFAULT_SERVER_URL)
    monkeypatch.setenv("PAIRCLI_CALLS", str(calls))
    monkeypatch.setattr(onboarding, "ensure_paircli", lambda home, output: binary)
    onboarding.grant_consent(tmp_path, "codex")

    result = onboarding.run(log, "codex")

    assert result == 0
    assert onboarding.read_status(tmp_path)["status"] == "complete"
    assert json.loads(calls.read_text()) == [["update", "--quiet", "--plugin-version", "0.1.0"], ["hook", "install", "codex"]]


def test_run_logs_in_again_after_server_change(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    auth = tmp_path / "auth.json"
    calls = tmp_path / "calls.json"
    log = tmp_path / "onboarding.log"
    auth.write_text(json.dumps({"access_token": "local-token", "server_url": "http://localhost:8080"}))
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PAIRCLI_SERVER_URL", "https://private.pair.example/")
    monkeypatch.setenv("PAIRCLI_CALLS", str(calls))
    monkeypatch.setattr(onboarding, "ensure_paircli", lambda home, output: binary)
    onboarding.grant_consent(tmp_path, "codex")

    assert onboarding.run(log, "codex") == 0
    assert json.loads(auth.read_text())["server_url"] == "https://private.pair.example"
    assert json.loads(calls.read_text())[-1] == ["login"]


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
    onboarding.grant_consent(tmp_path, "codex")

    def failing_update(command, *, cwd, log, timeout=onboarding.COMMAND_TIMEOUT_SECONDS):
        if command[1:] == ["update", "--quiet", "--plugin-version", "0.1.0"]:
            raise RuntimeError("command exited 1: paircli update")
        original_run_command(command, cwd=cwd, log=log, timeout=timeout)

    monkeypatch.setattr(onboarding, "run_command", failing_update)

    assert onboarding.run(log, "codex") == 0
    assert onboarding.read_status(tmp_path)["status"] == "complete"
    assert json.loads(calls.read_text()) == [["hook", "install", "codex"], ["login"]]
    assert "paircli update skipped" in log.read_text()


def test_uninstall_removes_the_scoped_hook_and_local_state(monkeypatch, tmp_path):
    binary = make_paircli(tmp_path)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "already-authorized"}))
    calls = tmp_path / "calls.json"
    monkeypatch.setenv("PAIRCLI_PLUGIN_HOME", str(tmp_path))
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PAIRCLI_CALLS", str(calls))
    onboarding.grant_consent(tmp_path, "codex")
    onboarding.write_release_metadata(tmp_path, version="0.1.0", target="darwin-arm64", checksum="0" * 64)

    result = onboarding.uninstall("codex")

    assert result["status"] == "uninstalled"
    assert json.loads(calls.read_text()) == [["hook", "uninstall", "codex"], ["logout"]]
    assert not binary.exists()
    assert not auth.exists()
    assert not onboarding.consent_path(tmp_path).exists()


def test_accepts_paircli_rfc3339_expiry_on_macos_python(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "access_token": "already-authorized",
                "server_url": onboarding.DEFAULT_SERVER_URL,
                "expires_at": "2027-01-03T15:58:58.95555+05:30",
            }
        )
    )
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.setenv("PAIRCLI_SERVER_URL", onboarding.DEFAULT_SERVER_URL)

    assert onboarding.has_active_login()


def test_has_active_login_requires_matching_backend(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    monkeypatch.setenv("PAIRCLI_AUTH_FILE", str(auth))
    monkeypatch.delenv("PAIRCLI_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PAIRCLI_SERVER_URL", raising=False)
    auth.write_text(json.dumps({"access_token": "local-token", "server_url": "http://localhost:8080"}))
    assert not onboarding.has_active_login()

    auth.write_text(json.dumps({"access_token": "production-token", "server_url": "https://prod-be.pair.sh/"}))
    assert onboarding.has_active_login()

    monkeypatch.setenv("PAIRCLI_SERVER_URL", "https://private.pair.example")
    assert not onboarding.has_active_login()
