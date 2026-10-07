from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "hook_pr_created.py"
SESSION_HOOK = Path(__file__).resolve().parent.parent / "hooks" / "hook_session_start.py"
HOOK_CONFIG = Path(__file__).resolve().parent.parent / "hooks" / "hooks.json"
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
    for key, value in (env_extra or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
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
    assert out["onboarding"]["status"] == "planned"


def test_hook_config_resolves_codex_and_claude_plugin_roots():
    config = json.loads(HOOK_CONFIG.read_text())
    command = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    startup = config["hooks"]["SessionStart"][0]

    assert "PLUGIN_ROOT" in command
    assert "CLAUDE_PLUGIN_ROOT" in command
    assert startup["matcher"] == "startup|resume"
    assert "PLUGIN_ROOT" in startup["hooks"][0]["command"]
    assert "CLAUDE_PLUGIN_ROOT" in startup["hooks"][0]["command"]


@pytest.mark.parametrize("harness,plugin_root", [("codex", "PLUGIN_ROOT"), ("claude-code", "CLAUDE_PLUGIN_ROOT")])
def test_session_start_plans_onboarding(harness, plugin_root, tmp_path):
    env = {key: value for key, value in os.environ.items() if key not in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")}
    env.update({plugin_root: "/plugins/pair", "PAIR_HOOK_DRY_RUN": "1", "PAIRCLI_PLUGIN_HOME": str(tmp_path)})

    result = subprocess.run([sys.executable, str(SESSION_HOOK)], text=True, capture_output=True, env=env, timeout=10)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Pair setup status: planned" in payload["systemMessage"]
    assert not (tmp_path / "onboarding.json").exists()


def test_session_start_skips_headless_pair_run(tmp_path):
    env = dict(os.environ, PLUGIN_ROOT="/plugins/pair", PAIR_RUN="1", PAIRCLI_PLUGIN_HOME=str(tmp_path))

    result = subprocess.run([sys.executable, str(SESSION_HOOK)], text=True, capture_output=True, env=env, timeout=10)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not (tmp_path / "onboarding.json").exists()


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


def test_accepts_codex_string_tool_response(tmp_path):
    out = run({"tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"},
               "tool_response": URL}, tmp=tmp_path)
    assert out["plan"]["key"] == "five-sh__awt__12"


def test_codex_resolves_pr_when_hook_omits_tool_response(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\nprint({URL!r})\n")
    gh.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()

    out = run(
        {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": f"cd {repo} && gh pr create --draft --fill"},
        },
        {"PLUGIN_ROOT": "/plugins/pair", "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        tmp=tmp_path,
    )

    assert out["onboarding"]["status"] == "planned"
    assert "Pair setup status: planned" in out["systemMessage"]


def test_ignores_ambiguous_pr_urls(tmp_path):
    out = run(event("gh pr create --fill", f"{URL}\nhttps://github.com/Five-SH/awt/pull/13\n"), tmp=tmp_path)
    assert out is None


def test_debug_event_redacts_command_and_response(tmp_path):
    debug_log = tmp_path / "event.json"
    out = run(
        event("gh pr create --fill --body 'secret'", f"{URL}\n"),
        {"PAIR_HOOK_EVENT_LOG": str(debug_log)},
        tmp=tmp_path,
    )

    assert out is not None
    payload = json.loads(debug_log.read_text())
    assert payload["tool_name"] == "Bash"
    assert payload["has_tool_response"] is True
    assert "tool_input" not in payload
    assert "tool_response" not in payload


def test_codex_starts_onboarding_without_claude_review(tmp_path):
    out = run(event("gh pr create --fill", f"{URL}\n"),
              {"PLUGIN_ROOT": "/plugins/pair"}, tmp=tmp_path)
    assert "plan" not in out
    assert out["onboarding"]["status"] == "planned"
    assert "Pair setup status: planned" in out["systemMessage"]
    assert "upload is not implemented" not in out["systemMessage"]


def test_codex_starts_setup_without_a_consent_block(tmp_path):
    out = run(
        event("gh pr create --fill", f"{URL}\n"),
        {
            "PAIR_HOOK_DRY_RUN": "0",
            "PAIRCLI_PLUGIN_HOME": str(tmp_path / "plugin"),
            "PLUGIN_ROOT": "/plugins/pair",
        },
        tmp=tmp_path,
    )

    assert "decision" not in out
    assert "Pair setup started for codex" in out["systemMessage"]


def test_codex_reports_prior_setup_failure_without_blocking(tmp_path):
    plugin_home = tmp_path / "plugin"
    plugin_home.mkdir()
    (plugin_home / "onboarding.json").write_text(json.dumps({
        "status": "failed",
        "error": "network unavailable",
        "log": "/tmp/pair-onboarding.log",
    }))

    out = run(
        event("gh pr create --fill", f"{URL}\n"),
        {
            "PAIR_HOOK_DRY_RUN": "0",
            "PAIRCLI_PLUGIN_HOME": str(plugin_home),
            "PLUGIN_ROOT": "/plugins/pair",
        },
        tmp=tmp_path,
    )

    assert "decision" not in out
    assert "continue" not in out
    assert "Pair setup previously failed" in out["systemMessage"]


def test_live_hook_starts_setup_automatically(tmp_path):
    out = run(
        event("gh pr create --fill", f"{URL}\n"),
        {"PAIR_HOOK_DRY_RUN": "0", "PAIRCLI_PLUGIN_HOME": str(tmp_path / "plugin")},
        tmp=tmp_path,
    )

    assert "Pair setup started for claude-code" in out["systemMessage"]
    assert not (tmp_path / "plugin" / "bin" / "paircli").exists()


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
    env = {"PAIR_HOOK_DRY_RUN": "0", "PAIRCLI_ONBOARDING_DISABLE": "1",
           "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
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
