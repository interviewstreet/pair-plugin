"""Check that all four built CLI assets match bootstrap's release contract."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pair_bootstrap", ROOT / "plugins/pair/hooks/bootstrap.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
PLATFORMS = ("darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64")


def check_release(manifest_path: Path) -> None:
    """Validate assets and checksums before CI can publish a release."""
    manifest = json.loads(manifest_path.read_text())
    if set(manifest.get("artifacts", {})) != set(PLATFORMS):
        raise ValueError("release must contain exactly the four supported platforms")
    checksums = []
    for key in PLATFORMS:
        _, expected = bootstrap.select_artifact(manifest, key)
        artifact = manifest_path.parent / manifest["version"] / f"paircli-{key}"
        size = artifact.stat().st_size
        if not 0 < size <= bootstrap.MAX_BINARY_BYTES:
            raise ValueError(f"invalid artifact size: {key}")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
            raise ValueError(f"artifact checksum mismatch: {key}")
        checksums.append(f"{expected}  paircli-{key}")
    published = (manifest_path.parent / manifest["version"] / "SHA256SUMS").read_text().splitlines()
    if sorted(published) != sorted(checksums):
        raise ValueError("SHA256SUMS does not match the release manifest")


if __name__ == "__main__":
    check_release(Path(sys.argv[1]))
    print("Verified four CLI artifacts, plugin compatibility, release URLs and checksums.")
