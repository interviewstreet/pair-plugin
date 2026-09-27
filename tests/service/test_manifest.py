"""Manifest construction: all five sections from queued onward, error is null not ''."""

import json
from datetime import datetime, timezone
from pathlib import Path

from pair.service.manifest import SECTION_NAMES, ManifestState

BUNDLE_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "bundle"


def _meta():
    return dict(
        key="interviewstreet__pair-be__62__3453326da379",
        repo="interviewstreet/pair-be",
        pr=62,
        title="Move the review build off the laptop and onto the workspace VM",
        base_sha="9f2c41ab77d3e6058b1c4a9de20f3b7c6d815e4a",
        head_sha="3453326da3791b0f4c8e2d5a6079bb31e4c7f8a2",
    )


def test_initial_manifest_matches_queued_fixture():
    stamp = datetime(2026, 9, 28, 0, 12, 7, tzinfo=timezone.utc)
    m = ManifestState.initial(**_meta(), now=stamp)
    expected = json.loads((BUNDLE_FIXTURES / "manifest-queued.json").read_text())
    assert m.to_dict() == expected


def test_running_matches_fixture_with_per_section_updates():
    stamp0 = datetime(2026, 9, 28, 0, 12, 7, tzinfo=timezone.utc)
    m = ManifestState.initial(**_meta(), now=stamp0)
    m.mark("side_effects", "done",
           now=datetime(2026, 9, 28, 0, 12, 33, tzinfo=timezone.utc))
    m.mark("pushback", "running",
           now=datetime(2026, 9, 28, 0, 12, 19, tzinfo=timezone.utc))
    m.mark("plan_vs_result", "done",
           now=datetime(2026, 9, 28, 0, 12, 41, tzinfo=timezone.utc))
    expected = json.loads((BUNDLE_FIXTURES / "manifest-running.json").read_text())
    assert m.to_dict() == expected


def test_failed_matches_fixture_with_non_null_error():
    stamp0 = datetime(2026, 9, 28, 0, 12, 7, tzinfo=timezone.utc)
    m = ManifestState.initial(**_meta(), now=stamp0)
    m.mark("side_effects", "done",
           now=datetime(2026, 9, 28, 0, 12, 33, tzinfo=timezone.utc))
    m.mark("plan_vs_result", "done",
           now=datetime(2026, 9, 28, 0, 12, 41, tzinfo=timezone.utc))
    m.mark(
        "pushback", "failed",
        error="Claude Code hit this account's session limit before the writer returned",
        now=datetime(2026, 9, 28, 0, 12, 48, tzinfo=timezone.utc),
    )
    expected = json.loads((BUNDLE_FIXTURES / "manifest-failed.json").read_text())
    assert m.to_dict() == expected


def test_all_five_sections_are_present_from_the_start():
    m = ManifestState.initial(**_meta())
    assert list(m.sections) == list(SECTION_NAMES)
    for state in m.sections.values():
        assert state.status == "queued"
        assert state.error is None  # never an empty string


def test_empty_string_error_is_normalised_to_null():
    m = ManifestState.initial(**_meta())
    m.mark("summary", "failed", error="")
    assert m.sections["summary"].error is None
