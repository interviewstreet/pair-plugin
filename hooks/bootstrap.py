"""SessionStart hook: install Pair CLI if needed, then run `paircli setup` detached.

The hook has a 5 s timeout, so it only starts a detached child (`bootstrap.py run <harness>`) and prints
one status line. The child downloads the binary when missing, then runs setup, which is idempotent and
does the self-update, hook install and device login.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_VERSION = "0.3.1"
MANIFEST_URL = "https://github.com/interviewstreet/paircli/releases/latest/download/latest.json"
MAX_BINARY_BYTES = 100 * 1024 * 1024
SETUP_LOG_GLOB = "~/.paircli/logs/setup-*.log"
LOGIN_HELP = f"approve the login in your browser; the device code and URL are in {SETUP_LOG_GLOB}"


def detect_harness(environ=os.environ) -> str:
    # Codex sets PLUGIN_ROOT for plugin hooks; Claude Code only sets CLAUDE_PLUGIN_ROOT.
    return "codex" if environ.get("PLUGIN_ROOT") else "claude-code"


def platform_key(system=None, machine=None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(machine)
    if system not in ("darwin", "linux") or arch is None:
        raise RuntimeError(f"paircli does not support {system}/{machine}")
    return f"{system}-{arch}"


def version_at_least(actual: str, required: str) -> bool:
    try:
        parse = lambda v: tuple(int(p) for p in v.split("+", 1)[0].split("-", 1)[0].split("."))
        return parse(actual) >= parse(required)
    except ValueError:
        return False


def select_artifact(manifest: dict, key: str) -> tuple:
    """Return (url, sha256) for one platform, after checking plugin compatibility."""
    required = manifest.get("min_plugin_version")
    if required is not None and not (isinstance(required, str) and version_at_least(PLUGIN_VERSION, required)):
        raise RuntimeError(f"paircli release requires Pair plugin {required} or newer")
    artifacts = manifest.get("artifacts")
    artifact = artifacts.get(key) if isinstance(artifacts, dict) else None
    if not isinstance(artifact, dict):
        raise RuntimeError(f"paircli release does not support {key}")
    url, digest = artifact.get("url"), str(artifact.get("sha256", "")).lower()
    if not isinstance(url, str) or not url.startswith("https://"):
        raise RuntimeError(f"paircli binary URL for {key} must use https")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise RuntimeError(f"paircli release has an invalid sha256 for {key}")
    return url, digest


def download(url: str, dest: Path, max_bytes: int, max_time: int = 300) -> None:
    """Fetch with the system curl so the user's own trust store and proxy settings apply."""
    subprocess.run(
        ["curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https", "--max-time", str(max_time),
         "--max-filesize", str(max_bytes), "-o", str(dest), url],
        check=True, capture_output=True, timeout=max_time + 10)


def install_binary(binary: Path, key: str, fetch=download) -> None:
    """Download, verify and atomically install paircli at `binary` with mode 0755."""
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        manifest_path = Path(scratch) / "latest.json"
        fetch(MANIFEST_URL, manifest_path, 1024 * 1024)
        url, digest = select_artifact(json.loads(manifest_path.read_text()), key)
    fd, name = tempfile.mkstemp(dir=str(binary.parent), prefix=".paircli-")
    os.close(fd)
    staged = Path(name)
    try:
        fetch(url, staged, MAX_BINARY_BYTES)
        data = staged.read_bytes()
        if len(data) > MAX_BINARY_BYTES:
            raise RuntimeError("paircli binary exceeds the size limit")
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError("paircli binary sha256 does not match the release manifest")
        os.chmod(staged, 0o755)
        os.replace(staged, binary)
    finally:
        if staged.exists():
            staged.unlink()


def is_installed(binary: Path) -> bool:
    return binary.is_file() and os.access(binary, os.X_OK)


def uses_organization_release(binary: Path) -> bool:
    """Identify installations already refreshed from the organization release source."""
    if not is_installed(binary):
        return False
    try:
        marker = binary.with_name("release-source.json")
        return json.loads(marker.read_text()).get("manifest_url") == MANIFEST_URL
    except (OSError, ValueError, AttributeError):
        return False


def record_release_source(binary: Path) -> None:
    """Record successful migration atomically; this marker is not a signature."""
    fd, name = tempfile.mkstemp(dir=str(binary.parent), prefix=".release-source-")
    staged = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"manifest_url": MANIFEST_URL}, stream)
        os.replace(staged, binary.with_name("release-source.json"))
    finally:
        if staged.exists():
            staged.unlink()


def run_child(harness: str, home: Path) -> int:
    binary = home / ".paircli" / "bin" / "paircli"
    if not uses_organization_release(binary):
        install_binary(binary, platform_key())
        record_release_source(binary)
    return subprocess.run([str(binary), "setup", "--harness", harness, "--plugin-version", PLUGIN_VERSION]).returncode


def start_child(harness: str, home: Path) -> Path:
    logs = home / ".paircli" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"bootstrap-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
    with open(log, "ab") as out:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", harness],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    return log


def preflight(installed: bool, which=None) -> tuple:
    """Return (blocker, note): one-line reasons for tools this machine lacks, or None.

    python3 is not checked: it is running this hook. curl only matters until the binary is installed, and
    without it setup cannot start (blocker). gh is only needed to publish, so its absence is a note.
    """
    which = which or shutil.which
    blocker = None if installed or which("curl") else (
        "curl not found, so the Pair CLI cannot be downloaded; install curl and start a new session")
    note = None if which("gh") else "gh not found; install the GitHub CLI and run `gh auth login` so Pair can publish PRs"
    return blocker, note


def status_line(binary: Path, harness: str) -> str:
    if not is_installed(binary):
        return f"Pair setup started: installing the Pair CLI, then {LOGIN_HELP}"
    if not uses_organization_release(binary):
        return f"Pair setup started: switching to the organization-owned CLI release, then {LOGIN_HELP}"
    try:
        out = subprocess.run([str(binary), "status", "--json"], capture_output=True, text=True, timeout=3)
        status = json.loads(out.stdout)
        logged_in = bool(status.get("logged_in"))
        hook = "installed" if (status.get("hooks") or {}).get(harness) else "pending"
        version = f" {status['cli_version']}" if status.get("cli_version") else ""
        line = f"Pair CLI{version}: {'logged in' if logged_in else 'login pending'}, hook {hook}"
        if not logged_in:
            log = (status.get("setup") or {}).get("log")
            line += f". To finish, approve the login in your browser; the device code and URL are in {log or SETUP_LOG_GLOB}"
        return line
    except Exception:
        return f"Pair setup started; if it asks for a login, {LOGIN_HELP}"


def hook_output(message: str) -> str:
    return json.dumps({"systemMessage": message,
                       "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message}})


def main(argv=None, home: Path = None) -> int:
    """Always returns 0: the hook must never block or fail a session."""
    argv, home = sys.argv[1:] if argv is None else argv, home or Path.home()
    if argv[:1] == ["run"]:
        try:
            run_child(argv[1], home)
        except Exception as error:
            print(f"pair bootstrap failed: {error}")
        return 0
    try:
        harness = detect_harness()
        binary = home / ".paircli" / "bin" / "paircli"
        blocker, note = preflight(uses_organization_release(binary))
        if blocker:
            message = f"Pair setup did not start: {blocker}"
        else:
            start_child(harness, home)
            message = status_line(binary, harness)
        if note:
            message += f". Note: {note}"
    except Exception as error:
        message = f"Pair setup could not start: {error}"
    print(hook_output(message))
    return 0


if __name__ == "__main__":
    sys.exit(main())
