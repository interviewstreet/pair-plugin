"""Render the Homebrew formula for a paircli release from its latest.json.

    python3 scripts/render-homebrew-formula.py cli/dist/paircli/latest.json > Formula/pair.rb

The formula is for a tap (`brew install interviewstreet/tap/pair`), then `paircli init`. It uses the
release's own binaries and checksums, after the same checks the plugin's bootstrap makes.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pair_bootstrap", ROOT / "plugins/pair/hooks/bootstrap.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)

TEMPLATE = '''class Pair < Formula
  desc "Publish a pull request with the coding-agent sessions that built it"
  homepage "https://github.com/interviewstreet/pair-plugin"
  version "{version}"

  on_macos do
    on_arm do
      url "{darwin_arm64_url}"
      sha256 "{darwin_arm64_sha}"
    end
    on_intel do
      url "{darwin_amd64_url}"
      sha256 "{darwin_amd64_sha}"
    end
  end

  on_linux do
    on_arm do
      url "{linux_arm64_url}"
      sha256 "{linux_arm64_sha}"
    end
    on_intel do
      url "{linux_amd64_url}"
      sha256 "{linux_amd64_sha}"
    end
  end

  def install
    bin.install Dir["paircli-*"].first => "paircli"
  end

  def caveats
    <<~EOS
      To set up Pair (hooks in Claude Code and Codex, approval in Codex, login), run:
        paircli init
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/paircli version")
  end
end
'''


def render(manifest: dict) -> str:
    values = {"version": manifest.get("version")}
    # min_plugin_version is about the plugin, not a brew install.
    checked = {k: v for k, v in manifest.items() if k != "min_plugin_version"} if isinstance(manifest, dict) else manifest
    for key in ("darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64"):
        url, digest = bootstrap.select_artifact(checked, key)
        values[key.replace("-", "_") + "_url"], values[key.replace("-", "_") + "_sha"] = url, digest
    return TEMPLATE.format(**values)


if __name__ == "__main__":
    sys.stdout.write(render(json.loads(Path(sys.argv[1]).read_text())))
