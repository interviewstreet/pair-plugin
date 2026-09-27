import json
import os
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


def run(ev, env_extra: dict | None = None, python: str = sys.executable, tmp: Path | None = None,
        plugin_root: str = "/plugins/pair"):
    env = {k: v for k, v in os.environ.items() if k not in ("PAIR_RUN", "PAIR_HOOK_DISABLE")}
    env.update(PAIR_HOOK_DRY_RUN="1", CLAUDE_PLUGIN_ROOT=plugin_root, PAIR_HOOK_SUBMIT_DELAY_S="0")
    if tmp:
        env.update(PAIR_HOME=str(tmp / "pair-home"), PAIR_CONFIG=str(tmp / "none.toml"))
    env.update(env_extra or {})
    p = subprocess.run([python, str(HOOK)], input=ev if isinstance(ev, str) else json.dumps(ev), text=True,
                       capture_output=True, env=env, timeout=15)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


@pytest.mark.parametrize("python", PYTHONS)
def test_pr_create_launches_pair_brief_submit(python, tmp_path):
    out = run(event("git push -u origin HEAD && gh pr create --fill --base main",
                    f"Creating pull request for feat into main\n\n{URL}\n"), python=python, tmp=tmp_path)
    plan = out["plan"]
    assert plan["key"] == "five-sh__awt__12"
    assert plan["repo"] == "five-sh/awt" and plan["pr"] == 12
    # argv, never a shell string. `submit` is a plain subcommand of pair-brief.
    assert plan["argv"] == ["/plugins/pair/bin/pair-brief", "submit", "five-sh/awt#12"]
    # The runner sleeps, then execs argv. Delay is 0 in tests, 10 by default in production.
    assert plan["delay_s"] == 0.0
    # The runner is invoked with the same python that ran the hook (its own sys.executable).
    # macOS `/usr/bin/python3` is a shim over Xcode's Python that resolves to the framework path,
    # so we just check the runner points at *some* python executable and uses `-c`.
    assert Path(plan["runner"][0]).name.startswith("python") and plan["runner"][1] == "-c"
    assert "time.sleep(float(sys.argv[1]))" in plan["runner"][2]
    assert "os.execvp(sys.argv[2], sys.argv[2:])" in plan["runner"][2]
    # Delay and argv follow the runner code, in that order.
    assert plan["runner"][3] == "0" and plan["runner"][4:] == plan["argv"]
    assert plan["cwd"] == str(tmp_path / "pair-home")
    assert plan["url"] == "http://127.0.0.1:7788/review/?key=five-sh__awt__12"
    assert "five-sh/awt#12" in out["systemMessage"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_default_delay_is_about_ten_seconds(tmp_path):
    # Drop PAIR_HOOK_SUBMIT_DELAY_S so the hook falls back to its production default.
    env = {"PAIR_HOOK_SUBMIT_DELAY_S": ""}
    out = run(event("gh pr create --fill", f"{URL}\n"), env_extra=env, tmp=tmp_path)
    assert out["plan"]["delay_s"] == 10.0
    assert out["plan"]["runner"][3] == "10"


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


def test_launch_detaches_and_execs_pair_brief(tmp_path):
    """Real launch path: the runner exec's a fake `pair-brief` that records how it was called."""
    plugin_root = tmp_path / "plugin"
    (plugin_root / "bin").mkdir(parents=True)
    fake = plugin_root / "bin" / "pair-brief"
    record = tmp_path / "called.json"
    # Fake pair-brief: dump argv + cwd + a scrubbed env so we can assert on all of it.
    fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}}, "
                    f"open({str(record)!r}, 'w'))\n")
    fake.chmod(0o755)
    env = {"PAIR_HOOK_DRY_RUN": "0", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
           "PAIR_HOOK_SUBMIT_DELAY_S": "0"}
    out = run(event("gh pr create --fill", f"{URL}\n"), env, tmp=tmp_path, plugin_root=str(plugin_root))
    assert "plan" not in out and "background" in out["hookSpecificOutput"]["additionalContext"]
    deadline = time.time() + 5
    while not record.exists() and time.time() < deadline:
        time.sleep(0.05)
    called = json.loads(record.read_text())
    assert called["argv"] == ["submit", "five-sh/awt#12"]
    assert Path(called["cwd"]).resolve() == (tmp_path / "pair-home").resolve()
    assert called["env"]["PAIR_RUN"] == "1"
    assert "CLAUDECODE" not in called["env"] and "CLAUDE_CODE_ENTRYPOINT" not in called["env"]
    assert list((tmp_path / "pair-home" / "logs").glob("pair-five-sh__awt__12-*.log"))


def test_delay_is_actually_awaited(tmp_path):
    """A non-zero delay defers the launch. Use 1s so the test stays fast."""
    plugin_root = tmp_path / "plugin"
    (plugin_root / "bin").mkdir(parents=True)
    fake = plugin_root / "bin" / "pair-brief"
    record = tmp_path / "called.json"
    fake.write_text(f"#!{sys.executable}\nimport json, sys, time\n"
                    f"json.dump({{'argv': sys.argv[1:], 'launched_at': time.time()}}, "
                    f"open({str(record)!r}, 'w'))\n")
    fake.chmod(0o755)
    env = {"PAIR_HOOK_DRY_RUN": "0", "PAIR_HOOK_SUBMIT_DELAY_S": "1"}
    started = time.time()
    out = run(event("gh pr create --fill", f"{URL}\n"), env, tmp=tmp_path, plugin_root=str(plugin_root))
    hook_returned = time.time()
    # The hook itself never blocks for the full delay: it just spawns and returns.
    assert hook_returned - started < 1.0
    assert out is not None
    deadline = time.time() + 5
    while not record.exists() and time.time() < deadline:
        time.sleep(0.05)
    launched_at = json.loads(record.read_text())["launched_at"]
    assert launched_at - started >= 0.9
