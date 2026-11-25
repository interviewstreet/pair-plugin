import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("render_formula", ROOT / "scripts/render-homebrew-formula.py")
render_formula = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_formula)
PLATFORMS = ("darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64")


def manifest(version="0.6.0"):
    return {"schema": "paircli-release/v1", "version": version, "revoked": [], "artifacts": {
        key: {"url": f"https://github.com/interviewstreet/pair-plugin/releases/download/v{version}/paircli-{key}",
              "sha256": f"{i:x}" * 64} for i, key in enumerate(PLATFORMS, start=1)}}


def test_formula_names_every_platform_binary_and_checksum():
    formula = render_formula.render(manifest())
    assert 'version "0.6.0"' in formula
    for i, key in enumerate(PLATFORMS, start=1):
        assert f'url "https://github.com/interviewstreet/pair-plugin/releases/download/v0.6.0/paircli-{key}"' in formula
        assert f'sha256 "{f"{i:x}" * 64}"' in formula
    assert 'bin.install Dir["paircli-*"].first => "paircli"' in formula
    assert "paircli init" in formula and '"#{bin}/paircli version"' in formula


def test_formula_ignores_min_plugin_version():
    release = manifest()
    release["min_plugin_version"] = "99.0.0"
    assert 'version "0.6.0"' in render_formula.render(release)


def test_formula_refuses_a_release_the_bootstrap_would_refuse():
    bad = manifest()
    bad["artifacts"]["linux-arm64"]["url"] = "https://example.com/paircli"
    with pytest.raises(RuntimeError):
        render_formula.render(bad)
    revoked = manifest()
    revoked["revoked"] = ["0.6.0"]
    with pytest.raises(RuntimeError, match="revoked"):
        render_formula.render(revoked)


def test_formula_is_valid_ruby(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("ruby"):
        pytest.skip("ruby not installed")
    formula = tmp_path / "pair.rb"
    formula.write_text(render_formula.render(manifest()))
    subprocess.run(["ruby", "-c", str(formula)], check=True, capture_output=True)
