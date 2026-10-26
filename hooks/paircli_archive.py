"""Package one PR commit's local Pair evidence without following symlinks."""
from __future__ import annotations

import os
import re
import tempfile
import shutil
import zipfile
from pathlib import Path


MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
# Also stored at the ZIP root, where ADE's extracted-bundle reader looks for them.
ROOT_ALIASES = ("generated_artifacts/architecture.json", "generated_artifacts/architecture.v2.json")


def _write_entry(archive, source: Path, name: str) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    with source.open("rb") as contents, archive.open(info, "w") as output:
        shutil.copyfileobj(contents, output)


def create_pr_archive(repo_dir: Path, artifact_dir: Path) -> Path:
    """Atomically zip the complete .paircli/pr-N/SHA folder beside itself."""
    repo_root = repo_dir.resolve(strict=True)
    if artifact_dir.is_symlink():
        raise ValueError("PR artifact folder must not be a symlink")
    folder = artifact_dir.resolve(strict=True)
    relative = folder.relative_to(repo_root)
    if len(relative.parts) != 3 or relative.parts[0] != ".paircli":
        raise ValueError("PR artifact folder must be under .paircli/pr-N/SHA")
    if not re.fullmatch(r"pr-[1-9][0-9]*", relative.parts[1]) or not re.fullmatch(
        r"[0-9a-f]{40}", relative.parts[2]
    ):
        raise ValueError("PR artifact folder must be under .paircli/pr-N/SHA")
    if any((repo_root / Path(*relative.parts[:index])).is_symlink() for index in (1, 2)):
        raise ValueError("PR artifact path must not contain symlinks")
    if not (folder / "summary.md").is_file() or (folder / "summary.md").is_symlink():
        raise ValueError("PR summary.md must exist before packaging")

    destination = folder.with_suffix(".zip")
    descriptor, temporary_name = tempfile.mkstemp(prefix="pair-archive-", suffix=".zip", dir=folder.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                _write_entry(archive, folder / "summary.md", "overview.md")
                for path in sorted(folder.rglob("*")):
                    # Unvalidated model output stays local; skip it before any symlink check.
                    if path.relative_to(folder).parts[0] == "diagnostics":
                        continue
                    if path.is_symlink():
                        raise ValueError(f"PR artifact contains a symlink: {path.relative_to(folder)}")
                    if path.is_file():
                        _write_entry(archive, path, path.relative_to(repo_root).as_posix())
                        relative_name = path.relative_to(folder).as_posix()
                        if relative_name in ROOT_ALIASES:
                            _write_entry(archive, path, relative_name)
                    elif not path.is_dir():
                        raise ValueError(f"PR artifact contains a non-file: {path.relative_to(folder)}")
        size = temporary.stat().st_size
        if size < 1 or size > MAX_ARCHIVE_BYTES:
            raise ValueError("PR artifact ZIP exceeds the 100 MiB upload limit")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
