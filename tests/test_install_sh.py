"""install.sh, run the way users run it: `cat install.sh | sh`, against a fake release server."""
import fcntl
import hashlib
import http.server
import json
import os
import platform
import pty
import select
import shutil
import subprocess
import termios
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.sh"
VERSION = "0.6.0"
PROD_MANIFEST = "https://github.com/interviewstreet/pair-plugin/releases/latest/download/latest.json"


def platform_key():
    system = {"Darwin": "darwin", "Linux": "linux"}[platform.system()]
    machine = platform.machine().lower()
    return f"{system}-{'arm64' if machine in ('arm64', 'aarch64') else 'amd64'}"


# A stand-in for paircli: `version`, and `init --install-self DIR` doing what
# the real one does to the file system. It logs every init call.
FAKE_PAIRCLI = """#!/bin/sh
if [ "$1" = version ]; then echo "paircli @VERSION@"; exit 0; fi
echo "$*" >> "$FAKE_LOG"
if [ "$2" = --install-self ]; then
  mkdir -p "$HOME/.paircli/bin"
  cp "$0" "$HOME/.paircli/bin/paircli"
  printf '{"manifest_url": "@MARKER@"}' > "$HOME/.paircli/bin/release-source.json"
  rm -rf "$3"
fi
"""


class Release:
    """A fake GitHub release: latest.json and one binary, counting requests."""

    def __init__(self, root, binary, version=VERSION):
        self.root, self.binary, self.version, self.requests = root, binary, version, []
        release = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                release.requests.append(self.path)
                if self.path == "/latest.json":
                    body, kind = json.dumps(release.manifest).encode(), "application/json"
                elif self.path == f"/download/v{release.version}/paircli-{platform_key()}":
                    body, kind = release.binary, "application/octet-stream"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.manifest = {
            "schema": "paircli-release/v1", "version": version, "revoked": [],
            "artifacts": {platform_key(): {"url": f"{self.url}/download/v{version}/paircli-{platform_key()}",
                                           "sha256": hashlib.sha256(binary).hexdigest()}},
        }

    def binary_requests(self):
        return [p for p in self.requests if p != "/latest.json"]


@pytest.fixture
def release(tmp_path):
    binary = FAKE_PAIRCLI.replace("@VERSION@", VERSION).replace("@MARKER@", PROD_MANIFEST).encode()
    r = Release(tmp_path, binary)
    yield r
    r.server.shutdown()


def environment(tmp_path, release, path=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "HOME": str(home), "PATH": path or os.environ["PATH"], "TMPDIR": str(tmp_path),
        "PAIR_INSTALL_TEST": "1", "PAIR_INSTALL_MANIFEST_URL": release.url + "/latest.json",
        "PAIR_INSTALL_RELEASE_PREFIX": release.url + "/download", "FAKE_LOG": str(tmp_path / "init.log"),
    }


def pipe_to_sh(env, script=None, args=(), timeout=60):
    """`cat install.sh | sh -s -- args`: the script arrives on stdin, with no terminal."""
    data = SCRIPT.read_bytes() if script is None else script
    return subprocess.run(["sh", "-s", "--", *args], input=data, env=env, capture_output=True,
                          timeout=timeout, start_new_session=True)


def init_calls(tmp_path):
    log = tmp_path / "init.log"
    return log.read_text().splitlines() if log.exists() else []


def test_installs_the_verified_binary_and_hands_over_to_init(tmp_path, release):
    env = environment(tmp_path, release)
    result = pipe_to_sh(env, args=["--no-login"])
    assert result.returncode == 0, result.stderr
    installed = tmp_path / "home/.paircli/bin/paircli"
    assert installed.read_bytes() == release.binary
    calls = init_calls(tmp_path)
    assert len(calls) == 1
    assert calls[0].startswith("init --install-self " + str(tmp_path / "pair-install."))
    assert calls[0].endswith("--enroll --grant-codex-trust --no-login")
    assert not list(tmp_path.glob("pair-install.*")), "init's install-self removes the temp dir"


def test_rerun_with_the_current_release_downloads_nothing(tmp_path, release):
    env = environment(tmp_path, release)
    assert pipe_to_sh(env).returncode == 0
    assert pipe_to_sh(env).returncode == 0
    assert len(release.binary_requests()) == 1
    assert init_calls(tmp_path)[1] == "init --enroll --grant-codex-trust"
    assert not list(tmp_path.glob("pair-install.*"))


@pytest.mark.parametrize("installed, downloads", [("0.5.0", 2), ("0.7.0", 1)], ids=["older", "newer"])
def test_rerun_replaces_an_older_binary_and_keeps_a_newer_one(tmp_path, release, installed, downloads):
    env = environment(tmp_path, release)
    assert pipe_to_sh(env).returncode == 0
    binary = tmp_path / "home/.paircli/bin/paircli"
    binary.write_text(binary.read_text().replace(f"paircli {VERSION}", f"paircli {installed}"))
    assert pipe_to_sh(env).returncode == 0
    assert len(release.binary_requests()) == downloads


def test_a_revoked_installed_version_is_always_replaced(tmp_path, release):
    env = environment(tmp_path, release)
    assert pipe_to_sh(env).returncode == 0
    binary = tmp_path / "home/.paircli/bin/paircli"
    binary.write_text(binary.read_text().replace(f"paircli {VERSION}", "paircli 0.9.0"))
    release.manifest["revoked"] = ["0.9.0"]
    assert pipe_to_sh(env).returncode == 0
    assert len(release.binary_requests()) == 2


def test_a_binary_without_the_release_marker_is_replaced(tmp_path, release):
    env = environment(tmp_path, release)
    assert pipe_to_sh(env).returncode == 0
    (tmp_path / "home/.paircli/bin/release-source.json").unlink()
    assert pipe_to_sh(env).returncode == 0
    assert len(release.binary_requests()) == 2


@pytest.mark.parametrize("change, message", [
    ({"sha256": "0" * 64}, "sha256 does not match"),
    ({"url": "https://example.com/paircli"}, "must reference this release"),
    ({"sha256": "abc"}, "invalid sha256"),
])
def test_rejects_a_bad_artifact(tmp_path, release, change, message):
    release.manifest["artifacts"][platform_key()].update(change)
    result = pipe_to_sh(environment(tmp_path, release))
    assert result.returncode != 0 and message in result.stderr.decode()
    assert not (tmp_path / "home/.paircli").exists() and init_calls(tmp_path) == []
    assert not list(tmp_path.glob("pair-install.*"))


@pytest.mark.parametrize("change, message", [
    ({"revoked": [VERSION]}, f"release {VERSION} is revoked"),
    ({"schema": "paircli-release/v2"}, "unsupported manifest schema"),
    ({"version": "latest"}, "invalid version"),
    ({"revoked": VERSION}, "invalid revoked list"),
    ({"revoked": None}, "invalid revoked list"),
    ({"artifacts": {}}, f"not available for {platform_key()}"),
])
def test_rejects_a_bad_manifest(tmp_path, release, change, message):
    release.manifest.update(change)
    result = pipe_to_sh(environment(tmp_path, release))
    assert result.returncode != 0 and message in result.stderr.decode()
    assert release.binary_requests() == [] and init_calls(tmp_path) == []


def test_a_binary_that_cannot_run_from_tmpdir_fails_clearly(tmp_path, release):
    release.binary = b"not an executable"
    release.manifest["artifacts"][platform_key()]["sha256"] = hashlib.sha256(release.binary).hexdigest()
    result = pipe_to_sh(environment(tmp_path, release))
    assert result.returncode != 0 and "mounted noexec?" in result.stderr.decode()
    assert not list(tmp_path.glob("pair-install.*")) and not (tmp_path / "home/.paircli").exists()


def test_rejects_an_unsupported_platform(tmp_path, release):
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "uname").write_text('#!/bin/sh\n[ "$1" = -s ] && echo FreeBSD || echo amd64\n')
    (fake_bin / "uname").chmod(0o755)
    result = pipe_to_sh(environment(tmp_path, release, path=f"{fake_bin}:{os.environ['PATH']}"))
    assert result.returncode != 0
    assert "macOS and Linux only; on Windows, use WSL" in result.stderr.decode()
    assert release.requests == []


def test_test_overrides_are_refused_outside_tests(tmp_path, release):
    env = environment(tmp_path, release)
    del env["PAIR_INSTALL_TEST"]
    result = pipe_to_sh(env)
    assert result.returncode != 0 and "for tests only" in result.stderr.decode()
    assert release.requests == []


def test_a_truncated_download_runs_nothing(tmp_path, release):
    script = SCRIPT.read_bytes()
    last_line = script.rindex(b"main ")
    for cut in (len(script) // 3, len(script) // 2, last_line - 1, last_line + 3):
        result = pipe_to_sh(environment(tmp_path, release), script=script[:cut])
        assert release.requests == [], f"cut at {cut} fetched {release.requests}"
        assert not (tmp_path / "home/.paircli").exists()
        assert init_calls(tmp_path) == []
        assert not list(tmp_path.glob("pair-install.*")), result


# --- the real paircli: init must not read stdin, and must not hang without a terminal ---

@pytest.fixture
def real_release(tmp_path):
    """A release serving the real paircli, built here (or taken from the release build)."""
    release_dir = os.environ.get("PAIRCLI_TEST_RELEASE_DIR")
    if release_dir:
        manifest = json.loads((Path(release_dir) / "latest.json").read_text())
        version = manifest["version"]
        built = Path(release_dir) / version / f"paircli-{platform_key()}"
    else:
        if not shutil.which("go"):
            pytest.skip("needs go, or PAIRCLI_TEST_RELEASE_DIR")
        built, version = tmp_path / "paircli-built", VERSION
        subprocess.run(["go", "build", "-ldflags", f"-X main.version={version}", "-o", str(built), "./cmd/paircli"],
                       cwd=ROOT / "cli", check=True, timeout=300)
    r = Release(tmp_path, built.read_bytes(), version=version)
    yield r
    r.server.shutdown()


def real_environment(tmp_path, release):
    env = environment(tmp_path, release, path="/usr/bin:/bin")
    home = Path(env["HOME"])
    (home / ".claude").mkdir()
    env.update({"PAIRCLI_SERVER_URL": "https://pair.invalid", "PAIRCLI_ALLOW_INSECURE_DOWNLOAD": "1",
                "PAIRCLI_UPDATE_MANIFEST_URL": release.url + "/latest.json"})
    return env


def assert_set_up(home, output):
    assert "Pair will set up:" in output, output
    assert "Pair: hooks installed for Claude Code" in output, output
    assert (home / ".paircli/bin/paircli").exists()
    assert json.loads((home / ".paircli/bin/release-source.json").read_text())["manifest_url"] == PROD_MANIFEST
    assert json.loads((home / ".paircli/state/init.json").read_text())["harnesses"]["claude-code"] == "installed"
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert any(" hook claude-code " in h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"])


def test_real_init_without_a_terminal(tmp_path, real_release):
    env = real_environment(tmp_path, real_release)
    result = pipe_to_sh(env, args=["--no-login"], timeout=120)
    output = result.stderr.decode()
    assert result.returncode == 0, output
    assert_set_up(Path(env["HOME"]), output)


def test_real_init_under_a_terminal(tmp_path, real_release):
    """`cat install.sh | sh` in a terminal: stdin is the pipe, /dev/tty is the pty."""
    env = real_environment(tmp_path, real_release)
    master, slave = pty.openpty()

    def controlling_terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(["sh", "-c", f"cat '{SCRIPT}' | sh -s -- --no-login"], env=env,
                            stdin=slave, stdout=slave, stderr=slave, preexec_fn=controlling_terminal)
    os.close(slave)
    output, deadline = b"", time.monotonic() + 120
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 1)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        elif proc.poll() is not None:
            break
    else:
        proc.kill()
        pytest.fail("install under a terminal did not finish:\n" + output.decode(errors="replace"))
    os.close(master)
    assert proc.wait(timeout=10) == 0, output.decode(errors="replace")
    assert_set_up(Path(env["HOME"]), output.decode(errors="replace").replace("\r\n", "\n"))
