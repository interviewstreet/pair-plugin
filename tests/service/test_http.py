"""HTTP surface: bearer auth, submit/reviews/health, contract fixtures."""

import json

import pytest

from pair.service.jobs import JobManager
from pair.service.storage import manifest_path

from .helpers import (
    AGENTS_DIR,
    BUNDLE_FIXTURES,
    TEST_KEY,
    TEST_META,
    TEST_TOKEN,
    auth_headers,
    build_payload,
    default_brief,
    default_evidence,
    default_writer_replies,
    run_and_wait,
)


# --------------------------------------------------------------------------- #
#  Bearer auth
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/health", "/submit", "/reviews/anything"])
def test_missing_bearer_returns_401(app_client, path):
    if path == "/submit":
        r = app_client.post(path, content=b"", headers={"Content-Type": "application/gzip"})
    else:
        r = app_client.get(path)
    assert r.status_code == 401
    assert r.json()["error"]


def test_wrong_bearer_returns_401(app_client):
    r = app_client.get("/health", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_malformed_bearer_returns_401(app_client):
    r = app_client.get("/health", headers={"Authorization": TEST_TOKEN})  # no "Bearer "
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
#  /health
# --------------------------------------------------------------------------- #


def test_health_200_when_login_check_true(app_client):
    r = app_client.get("/health", headers=auth_headers())
    assert r.status_code == 200


def test_health_503_when_login_check_false(storage, fake_runner):
    from fastapi.testclient import TestClient

    from pair.service.app import ServiceConfig, create_app

    manager = JobManager(
        storage=storage, runner=fake_runner, agents_dir=AGENTS_DIR,
        login_check=lambda: False, max_concurrent=2,
    )
    app = create_app(manager=manager, config=ServiceConfig(bearer_token=TEST_TOKEN))
    with TestClient(app) as client:
        r = client.get("/health", headers=auth_headers())
        assert r.status_code == 503


# --------------------------------------------------------------------------- #
#  /submit
# --------------------------------------------------------------------------- #


def _submit(client, body, *, force=None, content_type="application/gzip"):
    params = {}
    if force is not None:
        params["force"] = "true" if force else "false"
    return client.post(
        "/submit",
        content=body,
        params=params or None,
        headers={**auth_headers(), "Content-Type": content_type},
    )


def test_submit_returns_202_and_matches_accepted_fixture(app_client, manager):
    r = _submit(app_client, build_payload())
    assert r.status_code == 202
    expected = json.loads((BUNDLE_FIXTURES / "submit-response-accepted.json").read_text())
    assert r.json() == expected
    run_and_wait(manager, TEST_KEY)


def test_submit_stores_payload_before_ack(app_client, manager, storage):
    r = _submit(app_client, build_payload())
    # The payload must be recorded on storage before we returned 202.
    payload_writes = [p for p in storage.writes if p.startswith(f"payloads/{TEST_KEY}/")]
    assert payload_writes, "payload must be archived before acknowledging"
    assert r.status_code == 202
    run_and_wait(manager, TEST_KEY)


def test_submit_wrong_content_type_returns_415(app_client):
    r = _submit(app_client, b"whatever", content_type="application/json")
    assert r.status_code == 415


def test_submit_rejects_traversal(app_client):
    # Bad archive: traversal member.
    import io, tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="../oops.txt")
        info.type = tarfile.REGTYPE
        info.size = 3
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(b"bad"))
    r = _submit(app_client, buf.getvalue())
    assert r.status_code == 400


def test_submit_rejects_missing_meta(app_client):
    body = build_payload(include_meta=False)
    r = _submit(app_client, body)
    assert r.status_code == 400
    assert "meta.json" in r.json()["error"]


def test_submit_bad_key_in_meta_returns_400(app_client):
    body = build_payload(meta={**TEST_META, "key": "not-a-key"})
    r = _submit(app_client, body)
    assert r.status_code == 400


def test_submit_rejects_meta_with_an_extra_field(app_client):
    """meta.json is exactly six fields; a seventh means the two sides disagree."""
    r = _submit(app_client, build_payload(meta={**TEST_META, "bucket": "somewhere-else"}))
    assert r.status_code == 400
    assert "bucket" in r.json()["error"]


@pytest.mark.parametrize("field", ["base_sha", "head_sha"])
@pytest.mark.parametrize("bad", ["", "3453326da379", "3453326DA3791B0F4C8E2D5A6079BB31E4C7F8A2"])
def test_submit_rejects_shas_that_are_not_40_lowercase_hex(app_client, field, bad):
    # Both SHAs are copied into manifest.json verbatim, where the contract
    # requires 40 lowercase hex, so a short or upper-case SHA is rejected here.
    r = _submit(app_client, build_payload(meta={**TEST_META, field: bad}))
    assert r.status_code == 400
    assert field in r.json()["error"]


def test_submit_rejects_missing_meta_field(app_client):
    # The brief and evidence still come from the complete meta; only the
    # payload's meta.json is short a field.
    body = build_payload(
        evidence=default_evidence(TEST_META),
        brief=default_brief(TEST_META),
        meta={k: v for k, v in TEST_META.items() if k != "title"},
    )
    r = _submit(app_client, body)
    assert r.status_code == 400
    assert "title" in r.json()["error"]


def test_resubmit_finished_key_without_force_returns_200_and_matches_fixture(
    app_client, manager
):
    _submit(app_client, build_payload())
    run_and_wait(manager, TEST_KEY)
    r = _submit(app_client, build_payload())
    assert r.status_code == 200
    expected = json.loads((BUNDLE_FIXTURES / "submit-response-duplicate.json").read_text())
    assert r.json() == expected


def test_resubmit_finished_key_with_force_returns_202_and_starts_new_run(
    app_client, manager, storage, fake_runner
):
    _submit(app_client, build_payload())
    run_and_wait(manager, TEST_KEY)
    # Refill runner replies for the second run since FakeRunner pops.
    fake_runner.replies = default_writer_replies()
    r = _submit(app_client, build_payload(), force=True)
    assert r.status_code == 202
    run_and_wait(manager, TEST_KEY)
    # Manifest is still done after the forced re-run.
    manifest = json.loads(storage.get_bytes(manifest_path(TEST_KEY)).decode())
    assert manifest["status"] == "done"


def test_resubmit_still_archives_the_payload(app_client, manager, storage):
    _submit(app_client, build_payload())
    run_and_wait(manager, TEST_KEY)
    before = len([p for p in storage.writes if p.startswith(f"payloads/{TEST_KEY}/")])
    r = _submit(app_client, build_payload())
    assert r.status_code == 200
    after = len([p for p in storage.writes if p.startswith(f"payloads/{TEST_KEY}/")])
    assert after == before + 1, "duplicate submit must still be archived"


# --------------------------------------------------------------------------- #
#  /reviews/{key}
# --------------------------------------------------------------------------- #


def test_review_unknown_key_returns_404(app_client):
    good_key = "acme__demo__1__abcdef012345"
    r = app_client.get(f"/reviews/{good_key}", headers=auth_headers())
    assert r.status_code == 404


def test_review_bad_key_returns_400(app_client):
    r = app_client.get("/reviews/not-a-key", headers=auth_headers())
    assert r.status_code == 400


def test_review_running_returns_null_bundle_url(app_client, manager, storage):
    # Submit but do not wait — just prove the queued-state response shape.
    # Because our fake runner is synchronous, we instead let the job finish and
    # then manually rewrite the manifest back to queued to observe the shape.
    _submit(app_client, build_payload())
    run_and_wait(manager, TEST_KEY)
    # Rewrite manifest.json to the queued fixture body and assert the response.
    fixture = (BUNDLE_FIXTURES / "manifest-queued.json").read_bytes()
    storage.blobs[manifest_path(TEST_KEY)] = fixture
    r = app_client.get(f"/reviews/{TEST_KEY}", headers=auth_headers())
    assert r.status_code == 200
    body = r.json()
    expected = json.loads((BUNDLE_FIXTURES / "review-response-queued.json").read_text())
    # The response's manifest matches the queued fixture verbatim.
    assert body["manifest"] == expected["manifest"]
    assert body["bundle_url"] is None


def test_review_done_returns_signed_url_only_when_done(app_client, manager, storage):
    _submit(app_client, build_payload())
    run_and_wait(manager, TEST_KEY)
    r = app_client.get(f"/reviews/{TEST_KEY}", headers=auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["manifest"]["status"] == "done"
    assert body["bundle_url"] is not None
    assert "reviews/" + TEST_KEY + "/bundle.tar.gz" in body["bundle_url"]
    assert "exp=900" in body["bundle_url"]  # 15-minute TTL


def test_review_running_before_bundle_returns_null_url(app_client, storage):
    # Only manifest present at the prefix; no bundle.tar.gz yet.
    key = "acme__demo__9__abcdef012999"
    manifest = json.loads((BUNDLE_FIXTURES / "manifest-running.json").read_text())
    manifest["key"] = key
    storage.blobs[manifest_path(key)] = json.dumps(manifest).encode()
    r = app_client.get(f"/reviews/{key}", headers=auth_headers())
    assert r.status_code == 200
    assert r.json()["bundle_url"] is None


def test_review_done_but_no_bundle_returns_null_url(app_client, storage):
    # Corrupt state: manifest says done but bundle is missing. Contract says
    # bundle_url is only set when both hold.
    key = "acme__demo__42__abcdef012042"
    manifest = json.loads((BUNDLE_FIXTURES / "manifest-done.json").read_text())
    manifest["key"] = key
    storage.blobs[manifest_path(key)] = json.dumps(manifest).encode()
    r = app_client.get(f"/reviews/{key}", headers=auth_headers())
    assert r.status_code == 200
    assert r.json()["bundle_url"] is None
