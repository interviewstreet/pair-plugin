"""Checks the bundle fixtures against docs/bundle.md. The fixtures are what the other
streams copy, so a drift here is a drift in the contract."""
import json
import re
from pathlib import Path

import pytest

BUNDLE = Path(__file__).parent / "fixtures" / "bundle"

KEY_RX = re.compile(r"^[A-Za-z0-9._-]+__[A-Za-z0-9._-]+__[0-9]+__[0-9a-f]{12}$")
SHA_RX = re.compile(r"^[0-9a-f]{40}$")
RFC3339_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

STATUSES = {"queued", "running", "done", "failed"}
SECTIONS = {"summary", "look_first", "plan_vs_result", "pushback", "side_effects"}
MANIFEST_FIELDS = {"schema_version", "key", "repo", "pr", "title",
                   "base_sha", "head_sha", "status", "sections"}

MANIFESTS = ["manifest-queued.json", "manifest-running.json",
             "manifest-done.json", "manifest-failed.json"]
REVIEWS = ["review-response-queued.json", "review-response-done.json"]


def load(name: str) -> dict:
    return json.loads((BUNDLE / name).read_text())


def check_manifest(m: dict) -> None:
    assert set(m) == MANIFEST_FIELDS
    assert m["schema_version"] == 1
    assert KEY_RX.match(m["key"]), m["key"]
    assert m["status"] in STATUSES

    owner, repo, number, short = m["key"].split("__")
    assert m["repo"] == f"{owner}/{repo}"
    assert m["pr"] == int(number) > 0
    assert str(m["pr"]) == number  # no leading zeros
    assert SHA_RX.match(m["head_sha"]) and m["head_sha"][:12] == short
    assert SHA_RX.match(m["base_sha"]) and m["base_sha"] != m["head_sha"]
    assert isinstance(m["title"], str) and m["title"]

    assert set(m["sections"]) == SECTIONS
    for name, s in m["sections"].items():
        assert set(s) == {"status", "updated_at", "error"}, name
        assert s["status"] in STATUSES, name
        assert RFC3339_RX.match(s["updated_at"]), name
        # An empty error is written as null, never as "".
        assert s["error"] is None or (isinstance(s["error"], str) and s["error"]), name


@pytest.mark.parametrize("name", MANIFESTS)
def test_manifest_shape(name):
    check_manifest(load(name))


@pytest.mark.parametrize("name", MANIFESTS)
def test_top_status_agrees_with_sections(name):
    m = load(name)
    got = {s["status"] for s in m["sections"].values()}
    if m["status"] == "queued":
        assert got == {"queued"}
    elif m["status"] == "done":
        assert got == {"done"}
    elif m["status"] == "failed":
        assert "failed" in got and "running" not in got
    else:
        assert got & {"running", "done"} and got - {"done"}


def test_the_four_manifests_cover_the_enum():
    assert {load(n)["status"] for n in MANIFESTS} == STATUSES


def test_failed_manifest_explains_itself():
    sections = load("manifest-failed.json")["sections"]
    failed = [s for s in sections.values() if s["status"] == "failed"]
    assert failed and all(s["error"] for s in failed)
    assert all(s["error"] is None for s in sections.values() if s["status"] != "failed")


def test_every_fixture_uses_the_same_key():
    keys = {load(n)["key"] for n in MANIFESTS}
    keys |= {load(n)["manifest"]["key"] for n in REVIEWS}
    keys |= {load(n)["key"] for n in ("submit-response-accepted.json",
                                      "submit-response-duplicate.json")}
    assert keys == {"interviewstreet__pair-be__62__3453326da379"}


@pytest.mark.parametrize("name", REVIEWS)
def test_review_response_shape(name):
    r = load(name)
    assert set(r) == {"manifest", "bundle_url", "bundle_token"}
    check_manifest(r["manifest"])


def test_bundle_url_only_when_done():
    done = load("review-response-done.json")
    assert done["manifest"]["status"] == "done"
    url = done["bundle_url"]
    assert url == (
        "https://storage.googleapis.com/pair-poc2-reviews/reviews/"
        f"{done['manifest']['key']}/bundle.tar.gz"
    )
    assert "?" not in url
    assert done["bundle_token"] == "ya29.example-access-token"

    queued = load("review-response-queued.json")
    assert queued["manifest"]["status"] == "queued"
    assert queued["bundle_url"] is None
    assert queued["bundle_token"] is None


def test_submit_responses():
    assert load("submit-response-accepted.json") == {
        "key": "interviewstreet__pair-be__62__3453326da379"}
    # A resubmit of a finished key without force adds status and starts no job.
    dup = load("submit-response-duplicate.json")
    assert set(dup) == {"key", "status"} and dup["status"] == "done"
