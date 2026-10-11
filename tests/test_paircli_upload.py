import base64
import hashlib
import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from uuid import uuid4

import pytest


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))
SPEC = importlib.util.spec_from_file_location("paircli_upload", HOOKS_DIR / "paircli_upload.py")
assert SPEC and SPEC.loader
upload = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = upload
SPEC.loader.exec_module(upload)

COMMIT_HASH = "0123456789abcdef0123456789abcdef01234567"


def test_file_path_accepts_only_github_prs():
    assert upload._file_path("https://github.com/acme/repo/pull/42") == "acme/repo/42"
    for url in (
        "http://github.com/acme/repo/pull/42",
        "https://example.com/acme/repo/pull/42",
        "https://github.com/acme/repo/pull/0",
        "https://github.com/acme/repo/pull/2147483648",
    ):
        with pytest.raises(ValueError):
            upload._file_path(url)


def test_credential_uses_cli_token_and_local_backend(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "saved-token", "server_url": "http://localhost:8080"}))
    monkeypatch.delenv("PAIRCLI_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PAIRCLI_SERVER_URL", raising=False)

    assert upload._credential(auth) == ("saved-token", "http://localhost:8080")
    monkeypatch.setenv("PAIRCLI_AUTH_TOKEN", "override-token")
    assert upload._credential(auth)[0] == "override-token"


def test_credential_rejects_nonlocal_http(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "token", "server_url": "http://pair.example"}))
    monkeypatch.delenv("PAIRCLI_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PAIRCLI_SERVER_URL", raising=False)

    with pytest.raises(ValueError, match="HTTPS"):
        upload._credential(auth)


def test_credential_rejects_token_for_different_backend(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "local-token", "server_url": "http://localhost:8080"}))
    monkeypatch.delenv("PAIRCLI_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("PAIRCLI_SERVER_URL", "https://private.pair.example")

    with pytest.raises(RuntimeError, match="different backend"):
        upload._credential(auth)


def test_commit_hash_comes_from_archive_filename(tmp_path):
    archive = tmp_path / f"{COMMIT_HASH}.zip"
    archive.write_bytes(b"zip")
    assert upload._commit_hash(archive) == COMMIT_HASH

    with pytest.raises(ValueError, match="commit hash"):
        upload._commit_hash(tmp_path / "artifacts.zip")


def test_post_to_s3_streams_zip_last_without_pair_token(tmp_path, monkeypatch):
    archive = tmp_path / ("a" * 40 + ".zip")
    archive.write_bytes(b"zip-data")
    artifact_id = uuid4()
    signed = {
        "url": "https://bucket.s3.us-east-1.amazonaws.com",
        "fields": {
            "key": f"acme/repo/42/{artifact_id}.zip",
            "Content-Type": "application/zip",
            "policy": "signed-policy",
        },
    }
    sent = []

    class Response:
        status = 204

        def read(self, maximum):
            return b""

    class Connection:
        def __init__(self, host, port, timeout, context):
            assert host == "bucket.s3.us-east-1.amazonaws.com"
            assert context.check_hostname
            sent.append(self)

        def request(self, method, target, body, headers):
            self.method = method
            self.target = target
            self.headers = headers
            self.body = b"".join(body)

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(upload.http.client, "HTTPSConnection", Connection)

    upload._post_to_s3(signed, archive)

    connection = sent[0]
    assert connection.method == "POST"
    assert int(connection.headers["Content-Length"]) == len(connection.body)
    assert b'signed-policy' in connection.body
    assert connection.body.index(b'"file"') > connection.body.index(b'"policy"')
    assert b"zip-data" in connection.body
    assert "Authorization" not in connection.headers


def test_post_to_s3_rejects_untrusted_host(tmp_path):
    archive = tmp_path / ("a" * 40 + ".zip")
    archive.write_bytes(b"zip-data")

    with pytest.raises(ValueError, match="non-S3 HTTPS"):
        upload._post_to_s3({"url": "https://evil.example/upload", "fields": {}}, archive)


def test_upload_override_requires_authenticated_matching_backend(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    monkeypatch.setenv("PAIRCLI_UPLOAD_SERVER_URL", "https://private.pair.example")
    with pytest.raises(RuntimeError, match="login is required"):
        upload._upload_target(auth)
    auth.write_text(json.dumps({"access_token": "test-cli-token", "server_url": "https://pair.example"}))
    with pytest.raises(RuntimeError, match="different upload backend"):
        upload._upload_target(auth)
    monkeypatch.setenv("PAIRCLI_UPLOAD_SERVER_URL", "https://pair.example/")
    assert upload._upload_target(auth) == ("test-cli-token", "https://pair.example")


def metadata(revision=7):
    return {"owner": "acme", "repo": "repo", "pull_number": 42, "head_sha": COMMIT_HASH,
            "revision": revision, "sha256": hashlib.sha256(b"zip-data").hexdigest(), "size": 8}


def publication(data, state="reserved"):
    key = (f"v1/reviews/123/{data['pull_number']}/{data['head_sha']}/bundles/{uuid4()}/"
           f"{data['revision']}/{data['sha256']}.zip")
    return {**data, "publication_id": str(uuid4()), "review_id": str(uuid4()), "state": state,
            "upload": {"method": "POST", "url": "https://bucket.s3.us-east-1.amazonaws.com",
                       "expires_at": "2026-10-12T00:00:00Z", "fields": {
                           "key": key, "Content-Type": "application/zip", "x-amz-checksum-algorithm": "SHA256",
                           "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(data["sha256"])).decode(),
                       }} if state == "reserved" else None}


def finalized(reserved):
    result = {key: reserved[key] for key in
              ["publication_id", "review_id", "head_sha", "revision", "sha256", "size"]}
    return {**result, "state": "finalized", "finalized_at": "2026-10-11T00:00:00Z",
            "content_version": 1, "replayed": False}


def fake_backend(monkeypatch, responses):
    requests = []

    class Response(BytesIO):
        def __init__(self, status, payload):
            super().__init__(json.dumps({"data": payload}).encode())
            self.status = status

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            status, payload = response
            return Response(status, payload)

    monkeypatch.setattr(upload, "build_opener", lambda *_: Opener())
    return requests


@pytest.mark.parametrize("status", [200, 201])
def test_reservation_uses_exact_contract_and_cli_bearer(monkeypatch, status):
    data = metadata()
    result = publication(data)
    requests = fake_backend(monkeypatch, [(status, result)])
    assert upload._reserve("https://pair.example", "test-cli-token", data) == result
    assert requests[0].full_url == "https://pair.example/api/v1/reviews/publications/upload-url"
    assert requests[0].get_header("Authorization") == "Bearer test-cli-token"
    assert json.loads(requests[0].data) == data


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("sha256", "c" * 64),
                                         ("revision", 8), ("size", 9), ("pull_number", 43),
                                         ("publication_id", "not-a-uuid"), ("review_id", str(uuid4()))])
def test_reservation_rejects_mismatched_metadata(monkeypatch, field, value):
    data = metadata()
    result = publication(data)
    if field == "review_id":
        value = str(upload.UUID(int=0))
    result[field] = value
    fake_backend(monkeypatch, [(201, result)])
    with pytest.raises(RuntimeError, match="invalid|mismatched"):
        upload._reserve("https://pair.example", "test-cli-token", data)


@pytest.mark.parametrize("field,value", [("Content-Type", "text/plain"),
                                         ("x-amz-checksum-sha256", "wrong"),
                                         ("x-amz-checksum-algorithm", "MD5"), ("key", "legacy/key.zip")])
def test_reservation_rejects_wrong_checksum_or_object_key(monkeypatch, field, value):
    result = publication(metadata())
    result["upload"]["fields"][field] = value
    fake_backend(monkeypatch, [(201, result)])
    with pytest.raises(RuntimeError, match="invalid publication upload"):
        upload._reserve("https://pair.example", "test-cli-token", metadata())


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 409, 429, 503])
def test_api_errors_do_not_expose_provider_text_or_credentials(monkeypatch, status):
    error = HTTPError("https://pair.example", status, "private provider detail", {}, None)
    fake_backend(monkeypatch, [error])
    with pytest.raises(RuntimeError) as failure:
        upload._reserve("https://pair.example", "test-cli-token", metadata())
    assert "private provider detail" not in str(failure.value)
    assert "test-cli-token" not in str(failure.value)


@pytest.mark.parametrize("result", [None, [], {"publication_id": "invalid"}])
def test_malformed_reservation_fails_closed(monkeypatch, result):
    fake_backend(monkeypatch, [(201, result)])
    with pytest.raises(RuntimeError, match="invalid|mismatched"):
        upload._reserve("https://pair.example", "test-cli-token", metadata())


def test_revision_journal_replays_and_advances_across_heads_and_clock_rollback(tmp_path, monkeypatch):
    archive = tmp_path / (COMMIT_HASH + ".zip")
    monkeypatch.setattr(upload.time, "time_ns", lambda: 100)
    first = upload._revision(archive, "https://pair.example", metadata())
    assert first == 100
    assert upload._revision(archive, "https://pair.example", metadata()) == first
    next_data = {**metadata(), "head_sha": "b" * 40}
    monkeypatch.setattr(upload.time, "time_ns", lambda: 90)
    assert upload._revision(archive, "https://pair.example", next_data) == 101
    assert upload._revision(archive, "https://other.example", metadata()) == 90


def test_concurrent_revision_assignment_converges(tmp_path):
    archive = tmp_path / (COMMIT_HASH + ".zip")
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: upload._revision(archive, "https://pair.example", metadata()), range(16)))
    assert len(set(values)) == 1


def test_upload_reserve_s3_finalize_order_and_immutable_bytes(tmp_path, monkeypatch):
    archive = tmp_path / (COMMIT_HASH + ".zip")
    archive.write_bytes(b"zip-data")
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "test-cli-token", "server_url": "https://pair.example"}))
    monkeypatch.setattr(upload.time, "time_ns", lambda: 7)
    reserved = publication(metadata())
    requests = fake_backend(monkeypatch, [(201, reserved), (200, finalized(reserved))])
    observed = []

    def post(signed, snapshot):
        assert len(requests) == 1
        assert signed == reserved["upload"]
        archive.write_bytes(b"a different later scan")
        observed.append(snapshot.read_bytes())

    monkeypatch.setattr(upload, "_post_to_s3", post)
    assert upload.upload_archive("https://github.com/acme/repo/pull/42", archive, auth) == reserved["publication_id"]
    assert observed == [b"zip-data"]
    assert requests[1].full_url.endswith(f"/{reserved['publication_id']}/finalize")
    assert json.loads(requests[1].data) == {}
    assert all(r.get_header("Authorization") == "Bearer test-cli-token" for r in requests)


def test_finalized_reservation_skips_s3_and_replays_finalize(tmp_path, monkeypatch):
    archive = tmp_path / (COMMIT_HASH + ".zip")
    archive.write_bytes(b"zip-data")
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "test-cli-token", "server_url": "https://pair.example"}))
    monkeypatch.setattr(upload.time, "time_ns", lambda: 7)
    reserved = publication(metadata(), "finalized")
    requests = fake_backend(monkeypatch, [(200, reserved), (200, finalized(reserved))])
    monkeypatch.setattr(upload, "_post_to_s3", lambda *_: pytest.fail("finalized replay must not upload"))
    assert upload.upload_archive("https://github.com/acme/repo/pull/42", archive, auth) == reserved["publication_id"]
    assert len(requests) == 2


@pytest.mark.parametrize("failure_phase", ["s3", "finalize"])
def test_failed_publication_can_retry_same_revision(tmp_path, monkeypatch, failure_phase):
    archive = tmp_path / (COMMIT_HASH + ".zip")
    archive.write_bytes(b"zip-data")
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "test-cli-token", "server_url": "https://pair.example"}))
    monkeypatch.setattr(upload.time, "time_ns", lambda: 7)
    reserved = publication(metadata())
    responses = [(201, reserved)]
    if failure_phase == "finalize":
        responses.append(HTTPError("https://pair.example", 503, "private detail", {}, None))
    requests = fake_backend(monkeypatch, responses)

    def failed_post(*_):
        if failure_phase == "s3":
            raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(upload, "_post_to_s3", failed_post)
    with pytest.raises(RuntimeError):
        upload.upload_archive("https://github.com/acme/repo/pull/42", archive, auth)
    assert archive.read_bytes() == b"zip-data"
    fake_backend(monkeypatch, [(200, reserved), (200, finalized(reserved))])
    monkeypatch.setattr(upload, "_post_to_s3", lambda *_: None)
    assert upload.upload_archive("https://github.com/acme/repo/pull/42", archive, auth) == reserved["publication_id"]
    assert json.loads(requests[0].data)["revision"] == 7


@pytest.mark.parametrize("field,value", [("state", "reserved"), ("content_version", 0),
                                         ("finalized_at", None), ("publication_id", str(uuid4())),
                                         ("review_id", str(uuid4())), ("size", 9)])
def test_finalize_requires_confirmed_matching_publication(monkeypatch, field, value):
    reserved = publication(metadata())
    result = finalized(reserved)
    result[field] = value
    fake_backend(monkeypatch, [(200, result)])
    with pytest.raises(RuntimeError, match="confirm|mismatched"):
        upload._finalize("https://pair.example", "test-cli-token", reserved, metadata())


def test_credential_rejects_header_injection_without_printing_token(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"access_token": "test-token\r\nInjected: value",
                               "server_url": "https://pair.example"}))
    monkeypatch.delenv("PAIRCLI_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="invalid header characters") as error:
        upload._credential(auth)
    assert "test-token" not in str(error.value)


@pytest.mark.parametrize("raw", [b"invalid JSON", b"x" * (upload.MAX_RESPONSE_BYTES + 1)])
def test_invalid_or_oversized_api_body_fails_closed(monkeypatch, raw):
    class Response(BytesIO):
        status = 201

    class Opener:
        def open(self, *_, **__):
            return Response(raw)

    monkeypatch.setattr(upload, "build_opener", lambda *_: Opener())
    with pytest.raises(RuntimeError, match="invalid|too large"):
        upload._reserve("https://pair.example", "test-cli-token", metadata())
