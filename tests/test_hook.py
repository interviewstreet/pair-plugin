import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "hook_pr_created.py"
PYTHONS = [sys.executable] + (["/usr/bin/python3"] if Path("/usr/bin/python3").exists() else [])
URL = "https://github.com/Five-SH/awt/pull/12"


def event(command: str, stdout: str = "", **resp) -> dict:
    return {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": command, "description": "Open PR"},
            "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False, **resp},
            "tool_use_id": "toolu_1"}


def run(ev, env_extra: dict | None = None, python: str = sys.executable, tmp: Path | None = None):
    env = {k: v for k, v in os.environ.items() if k not in ("PAIR_RUN", "PAIR_HOOK_DISABLE")}
    env.update(PAIR_HOOK_DRY_RUN="1", CLAUDE_PLUGIN_ROOT="/plugins/pair")
    if tmp:
        env.update(PAIR_HOME=str(tmp / "pair-home"), PAIR_CONFIG=str(tmp / "none.toml"))
    env.update(env_extra or {})
    p = subprocess.run([python, str(HOOK)], input=ev if isinstance(ev, str) else json.dumps(ev), text=True,
                       capture_output=True, env=env, timeout=10)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


@pytest.mark.parametrize("python", PYTHONS)
def test_pr_create_starts_pair(python, tmp_path):
    out = run(event("git push -u origin HEAD && gh pr create --fill --base main",
                    f"Creating pull request for feat into main\n\n{URL}\n"), python=python, tmp=tmp_path)
    plan = out["plan"]
    assert plan["key"] == "five-sh__awt__12"
    assert plan["cmd"][1:4] == ["-p", "/pair five-sh/awt#12", "--plugin-dir"] and plan["cmd"][4] == "/plugins/pair"
    assert "--no-session-persistence" in plan["cmd"] and "Bash(pair-brief *)" in plan["cmd"]
    assert plan["cmd"][plan["cmd"].index("--setting-sources") + 1] == "project" and "--strict-mcp-config" in plan["cmd"]
    assert plan["cwd"] == str(tmp_path / "pair-home")
    assert plan["url"] == "http://127.0.0.1:7788/review/?key=five-sh__awt__12"
    assert "five-sh/awt#12" in out["systemMessage"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


@pytest.mark.parametrize("ev", [
    event("gh pr view 12", f"{URL}\n"),
    event("echo 'gh pr create'", "gh pr create\n"),
    event("gh pr create --web", "Opening github.com/five-sh/awt/pull/new/feat in your browser.\n"),
    event("gh pr create --fill", f"{URL}\n", interrupted=True),
    {**event("gh pr create --fill", f"{URL}\n"), "tool_name": "Read"},
    "not json",
])
def test_ignores_everything_else(ev, tmp_path):
    assert run(ev, tmp=tmp_path) is None


def test_guards(tmp_path):
    ev = event("gh pr create --fill", f"{URL}\n")
    assert run(ev, {"PAIR_RUN": "1"}, tmp=tmp_path) is None
    assert run(ev, {"PAIR_HOOK_DISABLE": "1"}, tmp=tmp_path) is None


def test_config_port(tmp_path):
    (tmp_path / "cfg.toml").write_text('host = "127.0.0.1"\nport = 7900\n')
    out = run(event("gh pr create --fill", f"{URL}\n"), {"PAIR_CONFIG": str(tmp_path / "cfg.toml")})
    assert out["plan"]["url"].startswith("http://127.0.0.1:7900/review/")


def test_launch_detaches_with_clean_env(tmp_path):
    """Real launch path, with a fake `claude` on PATH that records how it was called."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "called.json"
    fake = bin_dir / "claude"
    fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}}, "
                    f"open({str(record)!r}, 'w'))\n")
    fake.chmod(0o755)
    env = {"PAIR_HOOK_DRY_RUN": "0", "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
    out = run(event("gh pr create --fill", f"{URL}\n"), env, tmp=tmp_path)
    assert "plan" not in out and "being built" in out["hookSpecificOutput"]["additionalContext"]
    deadline = time.time() + 5
    while not record.exists() and time.time() < deadline:
        time.sleep(0.05)
    called = json.loads(record.read_text())
    assert called["argv"][:2] == ["-p", "/pair five-sh/awt#12"]
    assert Path(called["cwd"]).resolve() == (tmp_path / "pair-home").resolve()
    assert called["env"]["MAX_THINKING_TOKENS"] == "0" and called["env"]["PAIR_RUN"] == "1"
    assert "CLAUDECODE" not in called["env"] and "CLAUDE_CODE_ENTRYPOINT" not in called["env"]
    assert list((tmp_path / "pair-home" / "logs").glob("pair-five-sh__awt__12-*.log"))
    shutil.rmtree(bin_dir)
