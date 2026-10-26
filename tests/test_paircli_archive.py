import os
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
                "overview.md",
                f".paircli/pr-42/{'a' * 40}/summary.md",
                f".paircli/pr-42/{'a' * 40}/report.md",
                f".paircli/pr-42/{'a' * 40}/sessions/agent.json",
            ]
        )
        assert archive.read("overview.md") == b"summary"
        assert archive.read(f".paircli/pr-42/{'a' * 40}/summary.md") == b"summary"
    assert (folder / "summary.md").read_text() == "summary"


def test_excludes_unvalidated_diagnostics_from_zip(tmp_path):
    folder = tmp_path / ".paircli" / "pr-42" / ("a" * 40)
    (folder / "diagnostics").mkdir(parents=True)
    (folder / "summary.md").write_text("summary")
    (folder / "diagnostics" / "architecture-raw.txt").write_text("raw model output")
    (folder / "diagnostics" / "architecture-errors.json").write_text("[]")

    with zipfile.ZipFile(create_pr_archive(tmp_path, folder)) as archive:
        assert sorted(archive.namelist()) == sorted(
            ["overview.md", f".paircli/pr-42/{'a' * 40}/summary.md"]
        )


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


def test_archive_bytes_ignore_file_timestamps(tmp_path):
    folder = tmp_path / ".paircli" / "pr-42" / ("a" * 40)
    folder.mkdir(parents=True)
    summary = folder / "summary.md"
    summary.write_text("same evidence")
    first = create_pr_archive(tmp_path, folder).read_bytes()
    os.utime(summary, (1_000_000, 1_000_000))
    assert create_pr_archive(tmp_path, folder).read_bytes() == first


def test_both_architecture_files_get_a_zip_root_alias_and_diagnostics_stay_out(tmp_path):
    folder = tmp_path / ".paircli" / "pr-42" / ("a" * 40)
    (folder / "generated_artifacts").mkdir(parents=True)
    (folder / "diagnostics" / "architecture-llm").mkdir(parents=True)
    (folder / "summary.md").write_text("summary")
    (folder / "generated_artifacts" / "architecture.json").write_text('{"schema_version": 1}')
    (folder / "generated_artifacts" / "architecture.v2.json").write_text('{"schema_version": 2}')
    (folder / "generated_artifacts" / "other.json").write_text("{}")
    (folder / "diagnostics" / "architecture-v2-status.json").write_text("{}")
    (folder / "diagnostics" / "architecture-llm" / "stats.json").write_text("{}")

    with zipfile.ZipFile(create_pr_archive(tmp_path, folder)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert sorted(names) == sorted([
            "overview.md",
            f".paircli/pr-42/{'a' * 40}/summary.md",
            f".paircli/pr-42/{'a' * 40}/generated_artifacts/architecture.json",
            f".paircli/pr-42/{'a' * 40}/generated_artifacts/architecture.v2.json",
            f".paircli/pr-42/{'a' * 40}/generated_artifacts/other.json",
            "generated_artifacts/architecture.json",
            "generated_artifacts/architecture.v2.json",
        ])
        assert archive.read("generated_artifacts/architecture.v2.json") == b'{"schema_version": 2}'
        assert archive.read("generated_artifacts/architecture.json") == b'{"schema_version": 1}'


def test_v2_alone_is_aliased_when_v1_is_missing(tmp_path):
    folder = tmp_path / ".paircli" / "pr-42" / ("a" * 40)
    (folder / "generated_artifacts").mkdir(parents=True)
    (folder / "summary.md").write_text("summary")
    (folder / "generated_artifacts" / "architecture.v2.json").write_text("{}")

    with zipfile.ZipFile(create_pr_archive(tmp_path, folder)) as archive:
        assert "generated_artifacts/architecture.v2.json" in archive.namelist()
        assert "generated_artifacts/architecture.json" not in archive.namelist()
