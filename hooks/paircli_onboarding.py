#!/usr/bin/env python3
"""Install and authenticate paircli after the first agent-created pull request."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from pair_notification import notify_scan_result
from paircli_archive import create_pr_archive
from paircli_architecture import (
    llm_failure_reason, record_llm_diagram, remove_stale_architecture, write_architecture,
    write_fallback_architecture,
)
from paircli_architecture_llm import generate_v2
from paircli_architecture_v2 import finalize_v2, validate_v2
from paircli_architecture_v2_draft import (
    STATUS_V2, deterministic_v2_draft, remove_stale_architecture_v2, write_architecture_v2,
    write_no_architecture_v2,
)
from paircli_skeleton import build_skeleton
from paircli_summary import (
    EvidenceMissingError, STATUS_OUTPUT, agent_runner, stage_model, write_fallback_summary, write_summary,
)
from paircli_snapshot import captured_pr, count_files, files_without_line_data
from paircli_tls import compatible_tls_context
from paircli_upload import upload_archive


DEFAULT_REPOSITORY_URL = "https://github.com/rutvikchandla3/paircli"
DEFAULT_RELEASE_MANIFEST_URL = "https://github.com/rutvikchandla3/paircli/releases/latest/download/latest.json"
DEFAULT_SERVER_URL = "https://prod-be.pair.sh"
GITHUB_RELEASE_HOST = "github.com"
GITHUB_RELEASE_REPOSITORY = "rutvikchandla3/paircli"
GITHUB_RELEASE_PATH_PREFIX = "/rutvikchandla3/paircli/releases/"
GITHUB_RELEASE_ASSET_HOSTS = {
    "github-releases.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
COMMON_TOOL_DIRECTORIES = ("/opt/homebrew/bin", "/usr/local/bin")
MAX_BINARY_SIZE_BYTES = 100 * 1024 * 1024
RELEASE_DOWNLOAD_ATTEMPTS = 3
MANIFEST_TIMEOUT_SECONDS = 45
BINARY_TIMEOUT_SECONDS = 120
RELEASE_RETRY_DELAYS_SECONDS = (1, 3)
RELEASE_MANIFEST_ENV = "PAIRCLI_RELEASE_MANIFEST_URL"
DEVELOPMENT_BUILD_ENV = "PAIRCLI_DEV_BUILD"
SUPPORTED_HARNESSES = {"claude-code", "codex", "pi"}
COMMAND_TIMEOUT_SECONDS = 120
LOGIN_TIMEOUT_SECONDS = 16 * 60
SCAN_TIMEOUT_SECONDS = 16 * 60
METADATA_TIMEOUT_SECONDS = 20
LOCK_STALE_AFTER_SECONDS = 30 * 60
CONSENT_VERSION = "1"
PLUGIN_VERSION = "0.2.0"


def plugin_data_home() -> Path | None:
    configured = os.environ.get("PLUGIN_DATA")
    return Path(configured).expanduser() if configured else None


def onboarding_home() -> Path:
    configured = os.environ.get("PAIRCLI_PLUGIN_HOME")
    if configured:
        return Path(configured).expanduser()
    if plugin_data := plugin_data_home():
        return plugin_data / "paircli"
    return Path.home() / ".paircli" / "plugin"


def binary_path(home: Path) -> Path:
    return home / "bin" / "paircli"


def source_path(home: Path) -> Path:
    return home / "src"


def release_metadata_path(home: Path) -> Path:
    return home / "release.json"


def consent_path(home: Path) -> Path:
    return home / "consent.json"


def auth_file() -> Path:
    configured = os.environ.get("PAIRCLI_AUTH_FILE")
    return Path(configured).expanduser() if configured else onboarding_home().parent / "auth.json"


def server_url() -> str:
    return (os.environ.get("PAIRCLI_SERVER_URL") or DEFAULT_SERVER_URL).rstrip("/")


def parse_expiry(value: str) -> datetime:
    """Parse the RFC 3339 timestamp emitted by paircli on all supported Pythons."""
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        for pattern in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(normalized, pattern)
            except ValueError:
                continue
    raise ValueError(f"invalid RFC 3339 timestamp: {value}")


def status_path(home: Path) -> Path:
    return home / "onboarding.json"


def lock_path(home: Path) -> Path:
    return home / "onboarding.lock"


def pending_scan_path(home: Path) -> Path:
    return home / "pending-scan.json"


def scan_status_path(home: Path) -> Path:
    return home / "scan.json"


def read_status(home: Path) -> dict[str, Any]:
    try:
        value = json.loads(status_path(home).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_status(home: Path, status: str, **details: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat(), **details}
    temporary = status_path(home).with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(status_path(home))


def write_scan_status(home: Path, status: str, **details: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat(), **details}
    temporary = scan_status_path(home).with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(scan_status_path(home))


def read_consent(home: Path) -> dict[str, str]:
    try:
        value = json.loads(consent_path(home).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)}


def has_consent(home: Path, harness: str) -> bool:
    consent = read_consent(home)
    return consent.get("version") == CONSENT_VERSION and consent.get("harness") == harness


def grant_consent(home: Path, harness: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    temporary = consent_path(home).with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {"version": CONSENT_VERSION, "harness": harness, "approved_at": datetime.now(timezone.utc).isoformat()},
            indent=2,
        )
        + "\n"
    )
    temporary.replace(consent_path(home))


def has_active_login() -> bool:
    if os.environ.get("PAIRCLI_AUTH_TOKEN"):
        return True
    try:
        value = json.loads(auth_file().read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(value, dict) or not isinstance(value.get("access_token"), str) or not value["access_token"]:
        return False
    stored_server = value.get("server_url")
    if not isinstance(stored_server, str) or stored_server.rstrip("/") != server_url():
        return False
    expires_at = value.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at:
        return True
    try:
        expiry = parse_expiry(expires_at)
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > datetime.now(timezone.utc)


def clean_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "CLAUDECODE" and not key.startswith("CLAUDE_CODE_")
    }
    environment.setdefault("PAIRCLI_AUTH_FILE", str(auth_file()))
    environment["PAIRCLI_SERVER_URL"] = server_url()
    if codex_home := active_codex_home():
        environment.setdefault("CODEX_HOME", str(codex_home))
    path_entries = [directory for directory in COMMON_TOOL_DIRECTORIES if Path(directory).is_dir()]
    path_entries.extend(environment.get("PATH", "").split(os.pathsep))
    environment["PATH"] = os.pathsep.join(dict.fromkeys(entry for entry in path_entries if entry))
    return environment


def active_codex_home() -> Path | None:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    plugin_root = os.environ.get("PLUGIN_ROOT")
    if not plugin_root:
        return None
    for ancestor in Path(plugin_root).resolve().parents:
        if ancestor.name == "plugins":
            return ancestor.parent
    return None


def executable_path(name: str) -> str | None:
    return shutil.which(name, path=clean_environment().get("PATH"))


def platform_key() -> str:
    """Return the release-manifest key for the current operating system and CPU."""
    operating_system = platform.system().lower()
    architecture = platform.machine().lower()
    system_names = {"darwin": "darwin", "linux": "linux"}
    architecture_names = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
    if operating_system not in system_names or architecture not in architecture_names:
        raise RuntimeError(f"paircli does not support {operating_system}/{architecture}")
    return f"{system_names[operating_system]}-{architecture_names[architecture]}"


def require_https_url(value: str, *, label: str) -> str:
    """Validate release URLs before the plugin downloads executable content."""
    parsed = urlparse(value)
    is_local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    if parsed.scheme != "https" and not (is_local_http and os.environ.get("PAIRCLI_ALLOW_INSECURE_DOWNLOAD") == "1"):
        raise RuntimeError(f"{label} must use HTTPS")
    if not parsed.netloc:
        raise RuntimeError(f"{label} is invalid")
    return value


def is_github_release_asset_redirect(requested, final) -> bool:
    """Return whether a redirect targets this repository's GitHub release assets."""
    return (
        requested.scheme == "https"
        and final.scheme == "https"
        and (requested.hostname or "").lower() == GITHUB_RELEASE_HOST
        and requested.path.startswith(GITHUB_RELEASE_PATH_PREFIX)
        and (final.hostname or "").lower() in GITHUB_RELEASE_ASSET_HOSTS
    )


def verify_response_origin(response, requested_url: str, *, label: str) -> tuple[Any, Any]:
    """Allow only GitHub's expected release-asset redirects."""
    final_url = response.geturl() if hasattr(response, "geturl") else requested_url
    requested = urlparse(requested_url)
    final = urlparse(final_url)
    same_origin = (final.scheme, final.netloc) == (requested.scheme, requested.netloc)
    if not same_origin and not is_github_release_asset_redirect(requested, final):
        raise RuntimeError(f"{label} redirected to a different origin")
    return requested, final


def is_accepted_manifest_content_type(content_type: str, requested, final) -> bool:
    """Accept JSON, plus GitHub's unavoidable octet-stream release asset type."""
    return content_type == "application/json" or (
        content_type == "application/octet-stream" and is_github_release_asset_redirect(requested, final)
    )


def verify_json_response(response, requested_url: str) -> None:
    requested, final = verify_response_origin(response, requested_url, label="paircli release manifest")
    headers = getattr(response, "headers", None)
    content_type = headers.get_content_type() if headers is not None and hasattr(headers, "get_content_type") else ""
    if not is_accepted_manifest_content_type(content_type, requested, final):
        raise RuntimeError(f"paircli release manifest expected application/json, got {content_type or 'unknown'}")


def github_release_asset(url: str) -> tuple[str | None, str] | None:
    """Return the tag and asset name for this project's GitHub release URL."""
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != GITHUB_RELEASE_HOST or len(parts) != 6:
        return None
    if "/".join(parts[:3]) != f"{GITHUB_RELEASE_REPOSITORY}/releases":
        return None
    if parts[3:5] == ["latest", "download"]:
        tag = None
    elif parts[3] == "download" and parts[4]:
        tag = unquote(parts[4])
    else:
        return None
    asset = unquote(parts[5])
    if not asset or Path(asset).name != asset:
        return None
    return tag, asset


def fetch_github_release_asset(url: str, maximum_size: int) -> bytes | None:
    """Download this project's release asset through an authenticated GitHub CLI."""
    release_asset = github_release_asset(url)
    gh = executable_path("gh")
    if release_asset is None or gh is None:
        return None
    tag, asset = release_asset
    with tempfile.TemporaryDirectory(prefix="paircli-release-") as directory:
        command = [gh, "release", "download"]
        if tag is not None:
            command.append(tag)
        command.extend(["--repo", GITHUB_RELEASE_REPOSITORY, "--pattern", asset, "--dir", directory])
        try:
            completed = subprocess.run(
                command,
                env=clean_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=BINARY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("GitHub CLI timed out downloading the paircli release") from error
        if completed.returncode != 0:
            detail = completed.stdout.decode(errors="replace").strip()
            raise RuntimeError(f"GitHub CLI could not download the paircli release: {detail or 'unknown error'}")
        downloaded = Path(directory) / asset
        try:
            with downloaded.open("rb") as source:
                payload = source.read(maximum_size + 1)
        except OSError as error:
            raise RuntimeError(f"GitHub CLI did not produce the expected paircli release asset: {error}") from error
        if len(payload) > maximum_size:
            raise RuntimeError("paircli release asset exceeds the maximum download size")
        return payload


def fetch_json(url: str) -> dict[str, Any]:
    """Fetch a JSON release document from the configured release endpoint."""
    manifest_url = require_https_url(url, label="paircli release manifest URL")
    payload = fetch_github_release_asset(manifest_url, MAX_BINARY_SIZE_BYTES)
    if payload is None:
        for attempt in range(RELEASE_DOWNLOAD_ATTEMPTS):
            request = Request(manifest_url, headers={"User-Agent": "pair-plugin"})
            try:
                with urlopen(request, timeout=MANIFEST_TIMEOUT_SECONDS, context=compatible_tls_context()) as response:
                    verify_json_response(response, url)
                    payload = response.read(MAX_BINARY_SIZE_BYTES)
                break
            except OSError as error:
                if attempt + 1 == RELEASE_DOWNLOAD_ATTEMPTS:
                    raise RuntimeError(
                        f"could not fetch paircli release manifest after {RELEASE_DOWNLOAD_ATTEMPTS} attempts: {error}"
                    ) from error
                time.sleep(RELEASE_RETRY_DELAYS_SECONDS[attempt])
    try:
        document = json.loads(payload)
    except (TypeError, ValueError) as error:
        raise RuntimeError("paircli release manifest is not valid JSON") from error
    if not isinstance(document, dict):
        raise RuntimeError("paircli release manifest must be an object")
    return document


def release_artifact(manifest: dict[str, Any], target: str) -> tuple[str, str, str]:
    """Extract and validate one platform-specific binary declaration from a manifest."""
    version = manifest.get("version")
    artifacts = manifest.get("artifacts")
    artifact = artifacts.get(target) if isinstance(artifacts, dict) else None
    if not isinstance(version, str) or not version:
        raise RuntimeError("paircli release manifest is missing version")
    if not isinstance(artifact, dict):
        raise RuntimeError(f"paircli release {version} does not support {target}")
    url = artifact.get("url")
    checksum = artifact.get("sha256")
    if not isinstance(url, str) or not url:
        raise RuntimeError(f"paircli release {version} is missing a download URL for {target}")
    if (not isinstance(checksum, str) or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum.lower())):
        raise RuntimeError(f"paircli release {version} has an invalid SHA-256 for {target}")
    return version, require_https_url(url, label="paircli binary URL"), checksum.lower()


def version_at_least(actual: str, required: str) -> bool:
    """Compare stable three-part versions used by the plugin release contract."""
    try:
        actual_parts = tuple(int(part) for part in actual.split("+", 1)[0].split("-", 1)[0].split("."))
        required_parts = tuple(int(part) for part in required.split("+", 1)[0].split("-", 1)[0].split("."))
    except ValueError:
        return False
    return len(actual_parts) == len(required_parts) == 3 and actual_parts >= required_parts


def verify_plugin_compatibility(manifest: dict[str, Any]) -> None:
    required = manifest.get("min_plugin_version")
    if required is None:
        return
    if not isinstance(required, str) or not version_at_least(PLUGIN_VERSION, required):
        raise RuntimeError(f"paircli release requires Pair plugin {required} or newer")


def read_release_metadata(home: Path) -> dict[str, str]:
    try:
        value = json.loads(release_metadata_path(home).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)}


def write_release_metadata(home: Path, *, version: str, target: str, checksum: str) -> None:
    temporary = release_metadata_path(home).with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": version, "platform": target, "sha256": checksum}, indent=2) + "\n"
    )
    temporary.replace(release_metadata_path(home))


def download_binary(url: str, destination: Path, checksum: str) -> None:
    """Download, verify, and atomically install a paircli executable."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    github_payload = fetch_github_release_asset(url, MAX_BINARY_SIZE_BYTES)
    if github_payload is not None:
        if hashlib.sha256(github_payload).hexdigest() != checksum:
            raise RuntimeError("paircli binary checksum verification failed")
        descriptor, temporary_name = tempfile.mkstemp(prefix="paircli-", dir=str(destination.parent))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(github_payload)
            temporary.chmod(0o755)
            temporary.replace(destination)
            return
        finally:
            temporary.unlink(missing_ok=True)
    for attempt in range(RELEASE_DOWNLOAD_ATTEMPTS):
        descriptor, temporary_name = tempfile.mkstemp(prefix="paircli-", dir=str(destination.parent))
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                request = Request(url, headers={"User-Agent": "pair-plugin"})
                with urlopen(request, timeout=BINARY_TIMEOUT_SECONDS, context=compatible_tls_context()) as response:
                    verify_response_origin(response, url, label="paircli binary")
                    while chunk := response.read(64 * 1024):
                        total += len(chunk)
                        if total > MAX_BINARY_SIZE_BYTES:
                            raise RuntimeError("paircli binary exceeds the maximum download size")
                        digest.update(chunk)
                        output.write(chunk)
            if digest.hexdigest() != checksum:
                raise RuntimeError("paircli binary checksum verification failed")
            temporary.chmod(0o755)
            temporary.replace(destination)
            return
        except OSError as error:
            if attempt + 1 == RELEASE_DOWNLOAD_ATTEMPTS:
                raise RuntimeError(
                    f"could not download paircli binary after {RELEASE_DOWNLOAD_ATTEMPTS} attempts: {error}"
                ) from error
            time.sleep(RELEASE_RETRY_DELAYS_SECONDS[attempt])
        finally:
            temporary.unlink(missing_ok=True)


def run_command(command: list[str], *, cwd: Path, log, timeout: int = COMMAND_TIMEOUT_SECONDS) -> None:
    try:
        completed = subprocess.run(
            command, cwd=str(cwd), env=clean_environment(), stdout=log, stderr=subprocess.STDOUT, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"command timed out after {timeout}s: {' '.join(command)}") from error
    if completed.returncode != 0:
        raise RuntimeError(f"command exited {completed.returncode}: {' '.join(command)}")


def build_paircli_from_source(home: Path, log) -> Path:
    """Build paircli from a checkout for explicit local-development use only."""
    binary = binary_path(home)
    git = executable_path("git")
    if git is None:
        raise RuntimeError("git is required to install paircli")
    go = executable_path("go")
    if go is None:
        raise RuntimeError("Go is required to install paircli")
    source = source_path(home)
    if source.exists():
        if not (source / ".git").is_dir():
            raise RuntimeError(f"refusing to replace unexpected source directory: {source}")
    else:
        source.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [git, "clone", "--depth", "1",
             os.environ.get("PAIRCLI_REPOSITORY_URL", DEFAULT_REPOSITORY_URL), str(source)],
            cwd=source.parent,
            log=log,
        )
    binary.parent.mkdir(parents=True, exist_ok=True)
    run_command([go, "build", "-o", str(binary), "./cmd/paircli"], cwd=source, log=log)
    return binary


def ensure_paircli(home: Path, log) -> Path:
    """Install the current platform binary from the release manifest, or build only in dev mode."""
    if os.environ.get(DEVELOPMENT_BUILD_ENV) == "1":
        return build_paircli_from_source(home, log)

    binary = binary_path(home)
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    manifest = fetch_json(os.environ.get(RELEASE_MANIFEST_ENV, DEFAULT_RELEASE_MANIFEST_URL))
    verify_plugin_compatibility(manifest)
    target = platform_key()
    version, url, checksum = release_artifact(manifest, target)
    output = log if log is not None else sys.stderr
    output.write(f"downloading paircli {version} for {target}\n")
    download_binary(url, binary, checksum)
    write_release_metadata(home, version=version, target=target, checksum=checksum)
    return binary


def lock_is_stale(home: Path) -> bool:
    try:
        value = json.loads(lock_path(home).read_text())
    except (OSError, ValueError):
        return True
    if not isinstance(value, dict):
        return True
    created_at = value.get("created_at")
    pid = value.get("pid")
    try:
        created = parse_expiry(created_at) if isinstance(created_at, str) else None
    except ValueError:
        created = None
    if created is None or datetime.now(timezone.utc) - created > timedelta(seconds=LOCK_STALE_AFTER_SECONDS):
        return True
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def acquire_lock(home: Path) -> bool:
    home.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path(home), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not lock_is_stale(home):
            return False
        try:
            lock_path(home).unlink()
        except FileNotFoundError:
            pass
        return acquire_lock(home)
    with os.fdopen(descriptor, "w") as lock:
        json.dump({"pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}, lock)
    return True


def release_lock(home: Path) -> None:
    try:
        lock_path(home).unlink()
    except FileNotFoundError:
        pass


def log_file(home: Path) -> Path:
    directory = home / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"onboarding-{time.strftime('%Y%m%d-%H%M%S')}.log"


def scan_log_file(home: Path) -> Path:
    directory = home / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"scan-{time.strftime('%Y%m%d-%H%M%S')}.log"


def queue_scan(home: Path, pr_url: str | None, repo_dir: str | None) -> None:
    """Persist the PR scan requested by a hook until authentication is complete."""
    if not pr_url or not repo_dir:
        return
    home.mkdir(parents=True, exist_ok=True)
    temporary = pending_scan_path(home).with_suffix(".tmp")
    temporary.write_text(json.dumps({"pr_url": pr_url, "repo_dir": repo_dir}, indent=2) + "\n")
    temporary.replace(pending_scan_path(home))


def take_pending_scan(home: Path) -> dict[str, str] | None:
    """Consume one validated pending scan request, if present."""
    try:
        value = json.loads(pending_scan_path(home).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    pr_url, repo_dir = value.get("pr_url"), value.get("repo_dir")
    if not isinstance(pr_url, str) or not isinstance(repo_dir, str) or not pr_url or not repo_dir:
        return None
    pending_scan_path(home).unlink(missing_ok=True)
    return {"pr_url": pr_url, "repo_dir": repo_dir}


def launch_pending_scan(home: Path, binary: Path, harness: str) -> dict[str, str] | None:
    """Start a local snapshot scan without blocking the agent hook."""
    request = take_pending_scan(home)
    if request is None:
        return None
    repo_dir = Path(request["repo_dir"]).expanduser()
    log = scan_log_file(home)
    if not repo_dir.is_dir():
        message = f"scan repository does not exist: {repo_dir}"
        write_scan_status(home, "failed", error=message, log=str(log), pr_url=request["pr_url"])
        return {"status": "failed", "error": message, "log": str(log)}
    try:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "scan",
                "--log",
                str(log),
                "--pr-url",
                request["pr_url"],
                "--repo-dir",
                str(repo_dir),
                "--harness",
                harness,
            ],
            cwd=str(home),
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        queue_scan(home, request["pr_url"], str(repo_dir))
        write_scan_status(home, "failed", error=str(error), log=str(log), pr_url=request["pr_url"])
        return {"status": "failed", "error": str(error), "log": str(log)}
    write_scan_status(home, "running", log=str(log), pr_url=request["pr_url"])
    return {"status": "started", "log": str(log), "pr_url": request["pr_url"]}


def approval_command(harness: str) -> str:
    return f"{sys.executable} {Path(__file__).resolve()} approve --harness {harness}"


def start(
    harness: str, dry_run: bool = False, pr_url: str | None = None, repo_dir: str | None = None,
) -> dict[str, Any]:
    if os.environ.get("PAIRCLI_ONBOARDING_DISABLE") == "1":
        return {"status": "disabled"}
    if harness not in SUPPORTED_HARNESSES:
        return {"status": "unsupported", "harness": harness}
    home = onboarding_home()
    binary = binary_path(home)
    previous = read_status(home)
    if dry_run:
        return {
            "status": "planned",
            "binary": str(binary),
            "command": f"{sys.executable} {Path(__file__).resolve()} run --harness {harness}",
        }
    queue_scan(home, pr_url, repo_dir)
    if not has_consent(home, harness):
        grant_consent(home, harness)
    if previous.get("status") == "failed":
        return {"status": "failed", "error": previous.get("error", "unknown error"), "log": previous.get("log", "")}
    if previous.get("status") == "complete" and binary.is_file() and os.access(binary, os.X_OK) and has_active_login():
        result: dict[str, Any] = {"status": "complete", "binary": str(binary), "log": str(previous.get("log", ""))}
        if scan := launch_pending_scan(home, binary, harness):
            result["scan"] = scan
        return result
    if not acquire_lock(home):
        return {"status": "running", "log": str(read_status(home).get("log", ""))}
    log = log_file(home)
    write_status(home, "running", log=str(log))
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run", "--log", str(log), "--harness", harness],
            cwd=str(home), env=clean_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as error:
        release_lock(home)
        write_status(home, "failed", error=str(error), log=str(log))
        return {"status": "failed", "error": str(error), "log": str(log)}
    return {"status": "started", "log": str(log)}


def approve(harness: str) -> dict[str, str]:
    if harness not in SUPPORTED_HARNESSES:
        return {"status": "unsupported", "harness": harness}
    home = onboarding_home()
    grant_consent(home, harness)
    if read_status(home).get("status") == "failed":
        try:
            status_path(home).unlink()
        except FileNotFoundError:
            pass
    return start(harness)


def uninstall(harness: str) -> dict[str, str]:
    if harness not in SUPPORTED_HARNESSES:
        return {"status": "unsupported", "harness": harness}
    home = onboarding_home()
    binary = binary_path(home)
    log = log_file(home)
    try:
        with log.open("a", encoding="utf-8") as output:
            if binary.is_file() and os.access(binary, os.X_OK):
                run_command([str(binary), "hook", "uninstall", harness], cwd=home, log=output)
                run_command([str(binary), "logout"], cwd=home, log=output)
        for path in (binary, release_metadata_path(home), consent_path(home), lock_path(home),
                     pending_scan_path(home), scan_status_path(home)):
            path.unlink(missing_ok=True)
        auth_file().unlink(missing_ok=True)
        source = source_path(home)
        if source.exists():
            shutil.rmtree(source)
        write_status(home, "uninstalled", harness=harness, log=str(log))
        return {"status": "uninstalled", "log": str(log)}
    except Exception as error:
        write_status(home, "failed", error=str(error), log=str(log))
        return {"status": "failed", "error": str(error), "log": str(log)}


def run(log: Path, harness: str) -> int:
    home = onboarding_home()
    if not has_consent(home, harness):
        grant_consent(home, harness)
    try:
        with log.open("a", encoding="utf-8") as output:
            output.write("paircli plugin onboarding started\n")
            binary = ensure_paircli(home, output)
            try:
                run_command(
                    [str(binary), "update", "--quiet", "--plugin-version", PLUGIN_VERSION], cwd=home, log=output,
                )
            except RuntimeError as error:
                output.write(f"paircli update skipped: {error}\n")
            run_command([str(binary), "hook", "install", harness], cwd=home, log=output)
            if has_active_login():
                write_status(home, "complete", binary=str(binary), log=str(log))
                output.write("paircli is already logged in\n")
                launch_pending_scan(home, binary, harness)
                return 0
            output.write("opening paircli device login\n")
            run_command([str(binary), "login"], cwd=home, log=output, timeout=LOGIN_TIMEOUT_SECONDS)
            if not has_active_login():
                raise RuntimeError("login completed without a usable credential")
            write_status(home, "complete", binary=str(binary), log=str(log))
            launch_pending_scan(home, binary, harness)
            return 0
    except Exception as error:
        write_status(home, "failed", error=str(error), log=str(log))
        return 1
    finally:
        release_lock(home)


def artifact_directory(pr_url: str, repo_dir: Path) -> Path:
    """Resolve the PR head so CLI and plugin write to the same output folder."""
    match = re.fullmatch(r"https://github\.com/[A-Za-z0-9-]+/[\w.-]+/pull/(\d+)", pr_url)
    if match is None:
        raise RuntimeError(f"invalid GitHub PR URL: {pr_url}")
    commands = (
        ["git", "rev-parse", "--show-toplevel"],
        # headRefOid is GitHub's true head; the commits list stops at 100 entries.
        ["gh", "pr", "view", pr_url, "--json", "headRefOid", "--jq", ".headRefOid"],
    )
    results = []
    for command in commands:
        completed = subprocess.run(
            command, cwd=repo_dir, env=clean_environment(), text=True,
            capture_output=True, timeout=METADATA_TIMEOUT_SECONDS, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"could not resolve PR artifact folder: {' '.join(command)}: {completed.stderr.strip()}")
        results.append(completed.stdout.strip())
    root, head_commit = results
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head_commit):
        raise RuntimeError("GitHub returned an invalid PR head commit")
    return Path(root) / ".paircli" / f"pr-{match.group(1)}" / head_commit.lower()


def artifact_root(artifact_dir: Path) -> Path:
    """The git toplevel of `.paircli/pr-N/<sha>`; agents and packaging must work from there.

    The folder lives under the toplevel even when the hook ran in a subdirectory, and a restricted
    agent can only read inside its working directory.
    """
    parents = artifact_dir.parents
    if (len(parents) < 3 or parents[1].name != ".paircli" or not re.fullmatch(r"pr-[1-9][0-9]*", parents[0].name)
            or not re.fullmatch(r"[0-9a-f]{40}", artifact_dir.name)):
        raise RuntimeError(f"unexpected PR artifact folder: {artifact_dir}")
    return parents[2]


def run_summary_step(harness: str, pr_url: str, repo_dir: Path, artifact_dir: Path, log) -> Path:
    """Write summary.md, falling back to the deterministic one so it can never block the upload."""
    try:
        pr = captured_pr(artifact_dir)
        without_lines = files_without_line_data(pr)
        if without_lines:
            log.write(f"{len(without_lines)} of {count_files(pr)} files have no hunk data\n")
    except Exception:
        pass
    try:
        return write_summary(harness, pr_url, repo_dir, artifact_dir, log)
    except EvidenceMissingError:
        raise
    except Exception as error:
        log.write(f"summary generation failed unexpectedly ({type(error).__name__}: {error}); using fallback\n")
        return write_fallback_summary(pr_url, artifact_dir, [f"{type(error).__name__}: {error}"], log)


def summary_mode(artifact_dir: Path) -> str:
    """Read which path produced summary.md (llm, llm-retry or fallback) for the scan log."""
    try:
        status = json.loads((artifact_dir / "diagnostics" / STATUS_OUTPUT).read_text(encoding="utf-8"))
        return str(status["mode"])
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def run_diagram_step(harness: str, repo_dir: Path, artifact_dir: Path, log) -> Path | None:
    """Try the model's diagram, then a deterministic one; nothing here may fail the scan."""
    reason = ""
    try:
        generate = agent_runner(harness)
        diagram = write_architecture(artifact_dir, lambda prompt: generate(prompt, repo_dir), log)
        if diagram:
            record_llm_diagram(artifact_dir, log)
            return diagram
        reason = llm_failure_reason(artifact_dir)
    except Exception as error:
        log.write(f"architecture unavailable ({type(error).__name__}: {error}); continuing without an LLM diagram\n")
        reason = f"llm error: {type(error).__name__}"
        remove_stale_architecture(artifact_dir)
    try:
        return write_fallback_architecture(artifact_dir, reason, log)
    except Exception as error:
        log.write(f"architecture fallback failed ({type(error).__name__}: {error}); continuing without a diagram\n")
        remove_stale_architecture(artifact_dir)
        return None


def _v2_log(log, message: str) -> None:
    try:
        log.write(message if message.endswith("\n") else message + "\n")
    except Exception:
        pass


def _v2_no_diagram(artifact_dir: Path, reason: str, log) -> None:
    """Record tier "none" and remove any older v2 diagram; never raises."""
    try:
        remove_stale_architecture_v2(artifact_dir)
        write_no_architecture_v2(artifact_dir, reason, log)
    except Exception as error:
        _v2_log(log, f"architecture v2: could not record the skipped diagram ({type(error).__name__})")


def _v2_deterministic(draft, snapshot, head: str, artifact_dir: Path, reason: str, log) -> Path:
    document, _warnings = finalize_v2(draft, head, snapshot, "deterministic")
    path = write_architecture_v2(artifact_dir, document, log, tier="deterministic", reason=reason)
    _v2_log(log, f"architecture v2: tier=deterministic reason={reason}")
    return path


def run_diagram_v2_step(harness: str, repo_dir: Path, artifact_dir: Path, log) -> Path | None:
    """Write generated_artifacts/architecture.v2.json (schema v2); nothing here may raise or touch v1.

    Tiers: the model narrates the deterministic draft (`generate_v2`), else the draft itself is
    written, else no file. An older v2 diagram and status are removed first so a failed run never
    leaves a diagram that describes another run. PAIRCLI_ARCHITECTURE_V2=0 skips the step.
    """
    try:
        head = artifact_dir.name
        draft = snapshot = None
        try:
            remove_stale_architecture_v2(artifact_dir)
            if not (artifact_dir / "diagnostics").is_symlink():
                (artifact_dir / "diagnostics" / STATUS_V2).unlink(missing_ok=True)
            if os.environ.get("PAIRCLI_ARCHITECTURE_V2", "1").strip() == "0":
                _v2_log(log, "architecture v2: disabled by PAIRCLI_ARCHITECTURE_V2=0")
                return None
            try:
                snapshot = json.loads((artifact_dir / "snapshot.json").read_text(encoding="utf-8"))
                if not isinstance(snapshot, dict):
                    raise ValueError("snapshot.json is not an object")
            except (OSError, ValueError) as error:
                _v2_log(log, f"architecture v2: snapshot unreadable ({type(error).__name__}); no v2 diagram")
                _v2_no_diagram(artifact_dir, "snapshot unreadable", log)
                return None
            skeleton = build_skeleton(snapshot, head)
            draft = deterministic_v2_draft(skeleton, snapshot, head)
            if draft is None:
                reason = ("docs-only change" if skeleton.get("docs_only")
                          else "no citable lines" if skeleton.get("no_citable_lines") else "nothing to draw")
                _v2_log(log, f"architecture v2: tier=none reason={reason}")
                _v2_no_diagram(artifact_dir, reason, log)
                return None
            try:
                runner = agent_runner(harness)
            except Exception as error:
                _v2_log(log, f"architecture v2: no model available ({error}); using the deterministic diagram")
                return _v2_deterministic(draft, snapshot, head, artifact_dir, "llm unavailable", log)

            def generate(prompt, *, timeout, model):
                return runner(prompt, repo_dir, timeout=timeout, model=model)

            result = generate_v2(artifact_dir, snapshot, head, copy.deepcopy(draft), generate, log,
                                 harness=harness, skeleton=skeleton,
                                 group_model=stage_model("group"), merge_model=stage_model("merge"))
            document, stats = result["document"], result.get("stats") or {}
            validate_v2(document, head, snapshot)
            tier = "llm" if document.get("generated_by") == "llm" else "deterministic"
            if tier == "llm":
                reason = f"llm groups ok: {stats.get('groups_llm_ok')}/{stats.get('groups_selected')}, merge {stats.get('merge')}"
                if stats.get("degraded"):
                    reason += f" (degraded: {stats['degraded']})"
            else:
                reason = "no llm output applied"
            path = write_architecture_v2(artifact_dir, document, log, tier=tier, reason=reason, llm_calls=stats)
            _v2_log(log, f"architecture v2: tier={tier} groups_llm_ok={stats.get('groups_llm_ok')}/"
                         f"{stats.get('groups_selected')} merge={stats.get('merge')} "
                         f"steps_dropped={stats.get('steps_dropped')} cache_hits={stats.get('cache_hits')}")
            return path
        except Exception as error:
            _v2_log(log, f"architecture v2 failed ({type(error).__name__}: {str(error)[:200]}); trying the deterministic diagram")
            reason = f"v2 error: {type(error).__name__}"
            if draft is not None and snapshot is not None:
                try:
                    return _v2_deterministic(draft, snapshot, head, artifact_dir, reason, log)
                except Exception as fallback_error:
                    _v2_log(log, f"architecture v2 fallback failed ({type(fallback_error).__name__}); no v2 diagram")
            _v2_no_diagram(artifact_dir, reason, log)
            return None
    except Exception:
        return None


def scan(log: Path, pr_url: str, repo_dir: Path, harness: str) -> int:
    """Generate CLI evidence, then have the triggering agent write summary.md."""
    home = onboarding_home()
    binary = binary_path(home)
    artifact_dir = None
    try:
        with log.open("a", encoding="utf-8") as output:
            artifact_dir = artifact_directory(pr_url, repo_dir)
            root = artifact_root(artifact_dir)
            try:
                run_command(
                    [str(binary), "update", "--quiet", "--plugin-version", PLUGIN_VERSION],
                    cwd=repo_dir,
                    log=output,
                )
            except RuntimeError as error:
                output.write(f"paircli update skipped: {error}\n")
            output.write(f"generating local Pair scan artifacts for {pr_url}\n")
            run_command(
                [str(binary), "scan", pr_url, "--snapshot", "--out", str(artifact_dir)],
                cwd=repo_dir, log=output, timeout=SCAN_TIMEOUT_SECONDS,
            )
            (artifact_dir / "summary.md").unlink(missing_ok=True)
            output.write(f"generating AI summary.md with {harness}\n")
            summary = run_summary_step(harness, pr_url, root, artifact_dir, output)
            output.write(f"wrote {summary} (summary mode: {summary_mode(artifact_dir)})\n")
            diagram = run_diagram_step(harness, root, artifact_dir, output)
            output.write(f"architecture diagram: {'produced' if diagram else 'not produced'}\n")
            diagram_v2 = run_diagram_v2_step(harness, root, artifact_dir, output)
            output.write(f"architecture v2 diagram: {'produced' if diagram_v2 else 'not produced'}\n")
            archive = create_pr_archive(root, artifact_dir)
            output.write(f"packaged {archive}\n")
            artifact_id = ""
            if os.environ.get("PAIRCLI_UPLOAD_ENABLED", "1") != "0":
                artifact_id = upload_archive(pr_url, archive, auth_file())
                output.write(f"uploaded Pair artifact {artifact_id}\n")
        write_scan_status(home, "complete", log=str(log), pr_url=pr_url, summary=str(summary),
                          archive=str(archive), artifact_id=artifact_id)
        notify_scan_result(pr_url, succeeded=True, uploaded=bool(artifact_id))
        return 0
    except Exception as error:
        write_scan_status(home, "failed", error=str(error), log=str(log), pr_url=pr_url,
                          artifacts=str(artifact_dir) if artifact_dir else "",
                          summary=str(summary) if "summary" in locals() else "",
                          archive=str(archive) if "archive" in locals() else "")
        notify_scan_result(pr_url, succeeded=False)
        return 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    start_parser = subcommands.add_parser("start")
    start_parser.add_argument("--dry-run", action="store_true")
    start_parser.add_argument("--harness", required=True, choices=sorted(SUPPORTED_HARNESSES))
    approve_parser = subcommands.add_parser("approve")
    approve_parser.add_argument("--harness", required=True, choices=sorted(SUPPORTED_HARNESSES))
    uninstall_parser = subcommands.add_parser("uninstall")
    uninstall_parser.add_argument("--harness", required=True, choices=sorted(SUPPORTED_HARNESSES))
    run_parser = subcommands.add_parser("run")
    run_parser.add_argument("--log", required=True)
    run_parser.add_argument("--harness", required=True, choices=sorted(SUPPORTED_HARNESSES))
    scan_parser = subcommands.add_parser("scan")
    scan_parser.add_argument("--log", required=True)
    scan_parser.add_argument("--pr-url", required=True)
    scan_parser.add_argument("--repo-dir", required=True)
    scan_parser.add_argument("--harness", required=True, choices=sorted(SUPPORTED_HARNESSES))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv or sys.argv[1:])
    if arguments.command == "start":
        print(json.dumps(start(arguments.harness, arguments.dry_run)))
        return 0
    if arguments.command == "approve":
        print(json.dumps(approve(arguments.harness)))
        return 0
    if arguments.command == "uninstall":
        print(json.dumps(uninstall(arguments.harness)))
        return 0
    if arguments.command == "run":
        return run(Path(arguments.log), arguments.harness)
    return scan(Path(arguments.log), arguments.pr_url, Path(arguments.repo_dir), arguments.harness)


if __name__ == "__main__":
    sys.exit(main())
