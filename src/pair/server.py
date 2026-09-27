import os
import subprocess
import sys
import time
import urllib.request

from pair.config import Config, logs_dir


def healthy(cfg: Config, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(f"{cfg.base_url}/api/health", timeout=timeout) as r:
            return r.status == 200
    except OSError:
        return False


def server_env() -> dict[str, str]:
    """The environment for the detached server, without the variables of the Claude session that started it."""
    return {k: v for k, v in os.environ.items()
            if not (k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_") or k == "MAX_THINKING_TOKENS")}


def ensure_server(cfg: Config, wait_s: float = 8.0) -> dict:
    """Start `pair-brief serve` in the background unless it's already answering."""
    if healthy(cfg):
        return {"running": True, "started": False, "url": cfg.base_url}
    logs_dir().mkdir(parents=True, exist_ok=True)
    log_path = logs_dir() / "serve.log"
    log = open(log_path, "a")
    subprocess.Popen([sys.executable, "-m", "pair.cli", "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     start_new_session=True, cwd=os.path.expanduser("~"), env=server_env())
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if healthy(cfg):
            return {"running": True, "started": True, "url": cfg.base_url}
        time.sleep(0.2)
    return {"running": False, "started": True, "url": cfg.base_url, "log": str(log_path)}
