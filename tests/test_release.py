import hashlib
import importlib.util
import json
from pathlib import Path
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins/pair"
SPEC = importlib.util.spec_from_file_location("release_check", ROOT / "scripts/check-release-artifacts.py")
release_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_check)


def make_release(root):
    version = "0.5.0"
    directory = root / version
    directory.mkdir()
    document = {"schema": "paircli-release/v1", "version": version, "min_plugin_version": "0.4.0", "artifacts": {}}
    checksums = []
    for key in release_check.PLATFORMS:
        binary = ("binary-" + key).encode()
        digest = hashlib.sha256(binary).hexdigest()
        name = "paircli-" + key
        (directory / name).write_bytes(binary)
        checksums.append(f"{digest}  {name}")
        document["artifacts"][key] = {"url": f"https://github.com/interviewstreet/pair-plugin/releases/download/v{version}/{name}",
                                      "sha256": digest}
    (directory / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
    manifest = root / "latest.json"
    manifest.write_text(json.dumps(document))
    return manifest


def test_check_release_accepts_all_four_platforms(tmp_path):
    release_check.check_release(make_release(tmp_path))


def test_check_release_rejects_tampered_binary(tmp_path):
    manifest = make_release(tmp_path)
    (tmp_path / "0.5.0/paircli-linux-arm64").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        release_check.check_release(manifest)


def test_check_release_rejects_wrong_checksum_file(tmp_path):
    manifest = make_release(tmp_path)
    (tmp_path / "0.5.0/SHA256SUMS").write_text("wrong\n")
    with pytest.raises(ValueError, match="SHA256SUMS"):
        release_check.check_release(manifest)


def test_check_release_requires_all_four_platforms(tmp_path):
    manifest = make_release(tmp_path)
    document = json.loads(manifest.read_text())
    del document["artifacts"]["linux-arm64"]
    manifest.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="four supported platforms"):
        release_check.check_release(manifest)


def test_plugin_versions_match_bootstrap():
    version = release_check.bootstrap.PLUGIN_VERSION
    assert json.loads((PLUGIN_ROOT / ".claude-plugin/plugin.json").read_text())["version"] == version
    assert json.loads((PLUGIN_ROOT / ".codex-plugin/plugin.json").read_text())["version"].split("+", 1)[0] == version
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"] == version


def test_release_sources_share_one_repository():
    assert release_check.bootstrap.MANIFEST_URL == "https://github.com/interviewstreet/pair-plugin/releases/latest/download/latest.json"
    updater = (ROOT / "cli/internal/selfupdate/update.go").read_text()
    builder = (ROOT / "cli/scripts/build-release-artifacts.sh").read_text()
    assert release_check.bootstrap.MANIFEST_URL in updater
    assert "https://github.com/interviewstreet/pair-plugin/releases/download/v$VERSION" in builder
    assert "module github.com/interviewstreet/pair-plugin/cli" in (ROOT / "cli/go.mod").read_text()
    assert not (ROOT / "cli/.github").exists()
