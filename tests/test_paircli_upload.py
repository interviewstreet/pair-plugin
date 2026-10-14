import importlib.util
import json
import sys
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


def test_upload_target_uses_private_backend_without_local_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("PAIRCLI_UPLOAD_SERVER_URL", "https://dhruvi-pair.private.hackerrank.link/")
    assert upload._upload_target(tmp_path / "missing-auth.json") == (
        None,
        "https://dhruvi-pair.private.hackerrank.link",
    )


def test_upload_target_rejects_insecure_private_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("PAIRCLI_UPLOAD_SERVER_URL", "http://private.pair.example")
    with pytest.raises(ValueError, match="HTTPS"):
        upload._upload_target(tmp_path / "missing-auth.json")


def test_sign_upload_sends_bearer_and_checks_policy(monkeypatch):
    artifact_id = uuid4()
    file_path = "acme/repo/42"
    commit_hash = "a" * 40
    signed = {
        "id": str(artifact_id),
        "method": "POST",
        "filePath": file_path,
        "commitHash": commit_hash,
        "url": "https://bucket.s3.us-east-1.amazonaws.com",
        "fields": {
            "key": f"v1/reviews/{file_path}/{commit_hash}/artifacts.zip",
            "Content-Type": "application/zip",
        },
    }
    requests = []

    class Response(BytesIO):
        status = 201

    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return Response(json.dumps({"data": signed}).encode())

    monkeypatch.setattr(upload, "build_opener", lambda *_: Opener())

    assert upload._sign_upload("https://pair.example", "cli-token", file_path, commit_hash) == signed
    request, timeout = requests[0]
    assert request.get_header("Authorization") == "Bearer cli-token"
    assert json.loads(request.data) == {"filePath": file_path, "commitHash": commit_hash}
    assert timeout == upload.REQUEST_TIMEOUT_SECONDS


def test_sign_upload_to_private_backend_sends_no_bearer(monkeypatch):
    artifact_id = uuid4()
    file_path = "acme/repo/42"
    commit_hash = "b" * 40
    signed = {
        "id": str(artifact_id),
        "method": "POST",
        "filePath": file_path,
        "commitHash": commit_hash,
        "url": "https://bucket.s3.us-east-1.amazonaws.com",
        "fields": {
            "key": f"v1/reviews/{file_path}/{commit_hash}/artifacts.zip",
            "Content-Type": "application/zip",
        },
    }
    requests = []

    class Response(BytesIO):
        status = 201

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response(json.dumps({"data": signed}).encode())

    monkeypatch.setattr(upload, "build_opener", lambda *_: Opener())
    assert upload._sign_upload("https://private.pair.example", None, file_path, commit_hash) == signed
    assert requests[0].get_header("Authorization") is None
    assert requests[0].full_url == "https://private.pair.example/api/v1/upload/url"


def test_sign_upload_reports_forbidden_account(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(upload, "build_opener", lambda *_: Opener())

    with pytest.raises(RuntimeError, match="refused artifact upload for this account"):
        upload._sign_upload("http://localhost:8080", "cli-token", "acme/repo/42", "c" * 40)


def test_upload_archive_requires_commit_scoped_filename(tmp_path, monkeypatch):
    archive = tmp_path / "artifacts.zip"
    archive.write_bytes(b"zip-data")
    monkeypatch.setattr(upload, "_upload_target", lambda _: pytest.fail("must not request credentials"))

    with pytest.raises(ValueError, match="filename must be a commit hash"):
        upload.upload_archive(
            "https://github.com/acme/repo/pull/42",
            archive,
            tmp_path / "auth.json",
        )


def test_post_to_s3_streams_zip_last_without_pair_token(tmp_path, monkeypatch):
    archive = tmp_path / ("a" * 40 + ".zip")
    archive.write_bytes(b"zip-data")
    artifact_id = uuid4()
    signed = {
        "url": "https://bucket.s3.us-east-1.amazonaws.com",
        "fields": {
            "key": f"v1/reviews/acme/repo/42/{'a' * 40}/artifacts.zip",
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
        def __init__(self, host, port, timeout):
            assert host == "bucket.s3.us-east-1.amazonaws.com"
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
