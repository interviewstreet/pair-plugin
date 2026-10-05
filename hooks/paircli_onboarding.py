#!/usr/bin/env python3
"""Install and authenticate paircli after the first agent-created pull request."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPOSITORY_URL = "https://github.com/rutvikchandla3/paircli"
COMMON_TOOL_DIRECTORIES = ("/opt/homebrew/bin", "/usr/local/bin")


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


def auth_file() -> Path:
    configured = os.environ.get("PAIRCLI_AUTH_FILE")
    return Path(configured).expanduser() if configured else onboarding_home().parent / "auth.json"


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


def has_active_login() -> bool:
    if os.environ.get("PAIRCLI_AUTH_TOKEN"):
        return True
    try:
        value = json.loads(auth_file().read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(value, dict) or not isinstance(value.get("access_token"), str) or not value["access_token"]:
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


def run_command(command: list[str], *, cwd: Path, log) -> None:
    completed = subprocess.run(command, cwd=str(cwd), env=clean_environment(), stdout=log, stderr=subprocess.STDOUT)
    if completed.returncode != 0:
        raise RuntimeError(f"command exited {completed.returncode}: {' '.join(command)}")


def ensure_paircli(home: Path, log) -> Path:
    binary = binary_path(home)
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
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
            [git, "clone", "--depth", "1", os.environ.get("PAIRCLI_REPOSITORY_URL", DEFAULT_REPOSITORY_URL), str(source)],
            cwd=source.parent,
            log=log,
        )
    binary.parent.mkdir(parents=True, exist_ok=True)
    run_command([go, "build", "-o", str(binary), "./cmd/paircli"], cwd=source, log=log)
    return binary


def acquire_lock(home: Path) -> bool:
    home.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path(home), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w") as lock:
        lock.write(f"{os.getpid()}\n")
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


def start(dry_run: bool = False) -> dict[str, str]:
    if os.environ.get("PAIRCLI_ONBOARDING_DISABLE") == "1":
        return {"status": "disabled"}
    home = onboarding_home()
    binary = binary_path(home)
    if has_active_login() and binary.is_file() and os.access(binary, os.X_OK):
        return {"status": "complete", "binary": str(binary)}
    if dry_run:
        return {
            "status": "planned",
            "binary": str(binary),
            "command": f"{sys.executable} {Path(__file__).resolve()} run",
        }
    if not acquire_lock(home):
        return {"status": "running", "log": str(read_status(home).get("log", ""))}
    log = log_file(home)
    write_status(home, "running", log=str(log))
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run", "--log", str(log)],
            cwd=str(home), env=clean_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as error:
        release_lock(home)
        write_status(home, "failed", error=str(error), log=str(log))
        return {"status": "failed", "error": str(error), "log": str(log)}
    return {"status": "started", "log": str(log)}


def run(log: Path) -> int:
    home = onboarding_home()
    try:
        with log.open("a", encoding="utf-8") as output:
            output.write("paircli plugin onboarding started\n")
            binary = ensure_paircli(home, output)
            run_command([str(binary), "hook", "install"], cwd=home, log=output)
            if has_active_login():
                write_status(home, "complete", binary=str(binary), log=str(log))
                output.write("paircli is already logged in\n")
                return 0
            output.write("opening paircli device login\n")
            run_command([str(binary), "login"], cwd=home, log=output)
            if not has_active_login():
                raise RuntimeError("login completed without a usable credential")
            write_status(home, "complete", binary=str(binary), log=str(log))
            return 0
    except Exception as error:
        write_status(home, "failed", error=str(error), log=str(log))
        return 1
    finally:
        release_lock(home)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    start_parser = subcommands.add_parser("start")
    start_parser.add_argument("--dry-run", action="store_true")
    run_parser = subcommands.add_parser("run")
    run_parser.add_argument("--log", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv or sys.argv[1:])
    if arguments.command == "start":
        print(json.dumps(start(arguments.dry_run)))
        return 0
    return run(Path(arguments.log))


if __name__ == "__main__":
    sys.exit(main())
