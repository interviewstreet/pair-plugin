import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("binary_bootstrap", ROOT / "hooks/bootstrap.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


@pytest.mark.parametrize("existing", [False, True], ids=["fresh-install", "existing-install"])
def test_built_release_installs_and_runs_in_isolated_home(tmp_path, monkeypatch, existing):
    release_dir = os.environ.get("PAIRCLI_TEST_RELEASE_DIR")
    if not release_dir:
        pytest.skip("run after building release artifacts with PAIRCLI_TEST_RELEASE_DIR")
    release_dir = Path(release_dir).resolve()
    manifest = json.loads((release_dir / "latest.json").read_text())
    key = bootstrap.platform_key()
    payload = release_dir / manifest["version"] / ("paircli-" + key)
    home = tmp_path / "home"
    binary = home / ".paircli/bin/paircli"
    binary.parent.mkdir(parents=True)
    state = home / ".paircli/state/keep.json"
    state.parent.mkdir()
    state.write_text('{"publication":"retained"}')
    if existing:
        binary.write_bytes(b"old executable")
        binary.chmod(0o755)
    downloads, setup = [], []

    def fetch(url, destination, max_bytes, max_time=300):
        downloads.append(url)
        if url == bootstrap.MANIFEST_URL:
            destination.write_bytes((release_dir / "latest.json").read_bytes())
        else:
            assert url == manifest["artifacts"][key]["url"]
            destination.write_bytes(payload.read_bytes())

    install = bootstrap.install_binary
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "install_binary", lambda target, platform: install(target, platform, fetch=fetch))
        patch.setattr(bootstrap.subprocess, "run", lambda command: setup.append(command) or SimpleNamespace(returncode=0))
        assert bootstrap.run_child("codex", home) == 0
        assert bootstrap.run_child("codex", home) == 0
    assert downloads == [bootstrap.MANIFEST_URL, manifest["artifacts"][key]["url"]]
    assert len(setup) == 2
    assert setup[0] == [str(binary), "setup", "--harness", "codex", "--plugin-version", bootstrap.PLUGIN_VERSION]
    assert state.read_text() == '{"publication":"retained"}'
    assert bootstrap.uses_plugin_release(binary)
    environment = {"HOME": str(home), "PATH": os.environ.get("PATH", ""),
                   "XDG_CONFIG_HOME": str(home / ".config"), "PAIRCLI_SERVER_URL": "https://pair.invalid"}
    version = subprocess.run([str(binary), "version"], env=environment, capture_output=True, text=True, check=True, timeout=10)
    assert version.stdout.strip() == "paircli " + manifest["version"]
    status = subprocess.run([str(binary), "status", "--json"], env=environment, capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(status.stdout)["cli_version"] == manifest["version"]
    assert json.loads(status.stdout)["logged_in"] is False
