import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins/pair"
PACKAGE_FILES = {
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "LICENSE",
    "README.md",
    "assets/icon.png",
    "hooks/bootstrap.py",
    "hooks/hooks.json",
    "skills/pair/SKILL.md",
}


def test_installable_payload_has_only_runtime_files():
    files = set()
    for entry in PLUGIN_ROOT.rglob("*"):
        assert not entry.is_symlink(), entry
        if "__pycache__" in entry.parts:
            continue
        if entry.is_file():
            files.add(entry.relative_to(PLUGIN_ROOT).as_posix())
    assert files == PACKAGE_FILES
    assert (PLUGIN_ROOT / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert not (ROOT / ".claude-plugin/plugin.json").exists()
    assert not (ROOT / ".codex-plugin/plugin.json").exists()


def test_skill_does_not_preapprove_shell_access():
    content = (PLUGIN_ROOT / "skills/pair/SKILL.md").read_text()
    frontmatter = content.split("---", 2)[1]
    assert "allowed-tools:" not in frontmatter
    assert "paircli publish" in content
    assert "If permission is denied, stop; do not bypass it." in content


def test_installable_payload_stays_small_and_portable():
    names = set()
    for name in PACKAGE_FILES:
        assert name.casefold() not in names
        names.add(name.casefold())
        assert all(part not in {".", ".."} and not part.endswith((".", " ")) for part in Path(name).parts)
        assert not any(character in name for character in ':<>"|?*\\')
        assert (PLUGIN_ROOT / name).stat().st_size < 256 * 1024


@pytest.mark.parametrize("manifest_name", [".claude-plugin/plugin.json", ".codex-plugin/plugin.json"])
def test_manifest_assets_resolve_inside_installable_folder(manifest_name):
    manifest = json.loads((PLUGIN_ROOT / manifest_name).read_text())
    interface = manifest.get("interface", {})
    references = [manifest.get("icon"), interface.get("logo"), interface.get("composerIcon")]
    for reference in filter(None, references):
        assert reference.startswith("./")
        target = (PLUGIN_ROOT / reference).resolve()
        assert target.is_relative_to(PLUGIN_ROOT.resolve())
        assert target.is_file()


def test_installable_text_does_not_include_literal_test_credentials():
    credential = re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
    for name in PACKAGE_FILES - {"assets/icon.png"}:
        assert credential.search((PLUGIN_ROOT / name).read_text()) is None, name


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_copied_plugin_bootstraps_without_monorepo_files(tmp_path, monkeypatch, capsys, harness):
    installed = tmp_path / "plugin cache/pair"
    shutil.copytree(PLUGIN_ROOT, installed, ignore=shutil.ignore_patterns("__pycache__"))
    spec = importlib.util.spec_from_file_location("packaged_bootstrap", installed / "hooks/bootstrap.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    if harness == "codex":
        monkeypatch.setenv("PLUGIN_ROOT", str(installed))
    else:
        monkeypatch.delenv("PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(installed))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: f"/usr/bin/{name}")
    started = []
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda command, **kwargs: started.append((command, kwargs)))

    assert bootstrap.main([], home=tmp_path / "home") == 0

    output = json.loads(capsys.readouterr().out)
    assert "installing the Pair CLI" in output["systemMessage"]
    assert len(started) == 1
    command, options = started[0]
    assert command == [sys.executable, str(installed / "hooks/bootstrap.py"), "run", harness]
    assert options["start_new_session"] is True
