import sys
import zipfile
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from paircli_archive import create_pr_archive


def test_packages_only_one_complete_pr_commit(tmp_path):
    folder = tmp_path / ".paircli" / "pr-42" / ("a" * 40)
    (folder / "sessions").mkdir(parents=True)
    (folder / "summary.md").write_text("summary")
    (folder / "report.md").write_text("report")
    (folder / "sessions" / "agent.json").write_text("{}")
    other = tmp_path / ".paircli" / "pr-43" / ("b" * 40)
    other.mkdir(parents=True)
    (other / "summary.md").write_text("other")

    archive_path = create_pr_archive(tmp_path, folder)

    assert archive_path == folder.with_suffix(".zip")
    with zipfile.ZipFile(archive_path) as archive:
        assert sorted(archive.namelist()) == sorted(
            [
                f".paircli/pr-42/{'a' * 40}/summary.md",
                f".paircli/pr-42/{'a' * 40}/report.md",
                f".paircli/pr-42/{'a' * 40}/sessions/agent.json",
            ]
        )
        assert archive.read(f".paircli/pr-42/{'a' * 40}/summary.md") == b"summary"
    assert (folder / "summary.md").read_text() == "summary"


def test_rejects_symlinks_without_replacing_previous_archive(tmp_path):
    folder = tmp_path / ".paircli" / "pr-42" / ("a" * 40)
    folder.mkdir(parents=True)
    (folder / "summary.md").write_text("summary")
    (folder / "leak").symlink_to(tmp_path / "secret")
    folder.with_suffix(".zip").write_bytes(b"previous")

    with pytest.raises(ValueError, match="symlink"):
        create_pr_archive(tmp_path, folder)

    assert folder.with_suffix(".zip").read_bytes() == b"previous"


def test_requires_summary_and_commit_scoped_folder(tmp_path):
    folder = tmp_path / ".paircli" / "pr-42" / ("a" * 40)
    folder.mkdir(parents=True)

    with pytest.raises(ValueError, match="summary.md"):
        create_pr_archive(tmp_path, folder)

    (folder / "summary.md").write_text("summary")
    with pytest.raises(ValueError, match="PR artifact folder"):
        create_pr_archive(tmp_path, tmp_path / ".paircli" / "pr-42")
