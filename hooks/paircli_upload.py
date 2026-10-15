"""Publish one commit-scoped evidence ZIP through the review publication protocol."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import UUID

from paircli_archive import MAX_ARCHIVE_BYTES
from paircli_tls import compatible_tls_context


REQUEST_TIMEOUT_SECONDS = 20
UPLOAD_TIMEOUT_SECONDS = 5 * 60
MAX_RESPONSE_BYTES = 1024 * 1024


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _file_path(pr_url: str) -> str:
    parsed = urlparse(pr_url)
    match = re.fullmatch(
        r"/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/pull/([1-9][0-9]{0,9})/?",
        parsed.path,
    )
    if parsed.scheme != "https" or parsed.netloc != "github.com" or match is None:
        raise ValueError("upload requires a GitHub pull-request URL")
    if int(match.group(3)) > 2147483647:
        raise ValueError("upload requires a valid GitHub pull-request number")
    return "/".join(match.groups())


def _server_origin(server: str) -> str:
    parsed = urlparse(server)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    ):
        raise ValueError("Pair backend URL must use HTTPS outside localhost")
    if (not parsed.netloc or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("Pair backend URL must be an origin without credentials or a path")
    return server.rstrip("/")


def _credential(path: Path) -> tuple[str, str]:
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    token_override = os.environ.get("PAIRCLI_AUTH_TOKEN")
    token = token_override or stored.get("access_token")
    configured_server = os.environ.get("PAIRCLI_SERVER_URL")
    stored_server = stored.get("server_url")
    server = configured_server or stored_server
    if not isinstance(token, str) or not token or not isinstance(server, str) or not server:
        raise RuntimeError("Pair CLI login is required before artifact upload")
    if configured_server and not token_override and (
        not isinstance(stored_server, str) or stored_server.rstrip("/") != configured_server.rstrip("/")
    ):
        raise RuntimeError("Pair CLI login belongs to a different backend; log in to this Pair backend")
    if "\r" in token or "\n" in token:
        raise RuntimeError("Pair CLI credential contains invalid header characters")
    return token, _server_origin(server)


def _upload_target(auth_path: Path) -> tuple[str, str]:
    token, server = _credential(auth_path)
    if upload_server := os.environ.get("PAIRCLI_UPLOAD_SERVER_URL"):
        if _server_origin(upload_server) != server:
            raise RuntimeError("Pair CLI login belongs to a different upload backend; log in there first")
    return token, server


def _commit_hash(archive: Path) -> str:
    match = re.fullmatch(r"([0-9a-f]{40})\.zip", archive.name)
    if match is None:
        raise ValueError("PR artifact ZIP filename must be a commit hash")
    return match.group(1)


def _request_json(server: str, token: str, path: str, payload: dict, statuses: set[int]) -> dict:
    request = Request(
        server + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        opener = build_opener(NoRedirects(), HTTPSHandler(context=compatible_tls_context()))
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status not in statuses:
                raise RuntimeError(f"Pair rejected evidence publication (HTTP {response.status})")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        messages = {
            401: "Pair CLI login expired or was rejected; run paircli login and retry publication",
            403: "Pair refused evidence publication; check GitHub App authorization and repository access",
            404: "Pair review publication or repository is unavailable",
            409: "Pair publication conflicts with server state; preserve the ZIP and resolve the revision conflict",
        }
        raise RuntimeError(messages.get(error.code, f"Pair rejected evidence publication (HTTP {error.code})"))
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Pair publication response is too large")
    try:
        result = json.loads(raw)["data"]
        if not isinstance(result, dict):
            raise ValueError("invalid publication")
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Pair returned an invalid publication response") from error
    return result


def _revision(archive: Path, server: str, metadata: dict) -> int:
    # The journal contains only bundle metadata. Replays keep their revision;
    # new content advances across heads, even if the local clock moves back.
    journal = archive.parent / ".publication-revisions.sqlite3"
    with sqlite3.connect(journal, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS revisions (
            server TEXT, head TEXT, digest TEXT, size INTEGER, revision INTEGER,
            PRIMARY KEY (server, head, digest, size))""")
        values = (server, metadata["head_sha"], metadata["sha256"], metadata["size"])
        row = db.execute(
            "SELECT revision FROM revisions WHERE server=? AND head=? AND digest=? AND size=?", values,
        ).fetchone()
        if row:
            return row[0]
        latest = db.execute("SELECT COALESCE(MAX(revision), 0) FROM revisions WHERE server=?", (server,))
        revision = max(time.time_ns(), latest.fetchone()[0] + 1)
        if not 0 < revision < 2**63:
            raise RuntimeError("Local publication revision is outside the backend integer range")
        db.execute("INSERT INTO revisions VALUES (?, ?, ?, ?, ?)", (*values, revision))
        return revision


def _validate_publication(publication: dict, metadata: dict) -> None:
    try:
        for field in ("publication_id", "review_id"):
            if UUID(publication[field]).int == 0:
                raise ValueError("missing publication identity")
        for field in ("head_sha", "revision", "sha256", "size"):
            if type(publication[field]) is not type(metadata[field]) or publication[field] != metadata[field]:
                raise ValueError("publication metadata mismatch")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise RuntimeError("Pair returned mismatched publication metadata") from error


def _reserve(server: str, token: str, metadata: dict) -> dict:
    publication = _request_json(server, token, "/api/v1/reviews/publications/upload-url", metadata, {200, 201})
    _validate_publication(publication, metadata)
    try:
        if (type(publication["pull_number"]) is not int
                or publication["pull_number"] != metadata["pull_number"]):
            raise ValueError("different pull request")
        for field in ("owner", "repo"):
            if not isinstance(publication[field], str) or not publication[field].strip():
                raise ValueError("missing canonical coordinates")
        if publication["state"] == "finalized" and publication["upload"] is None:
            return publication
        signed = publication["upload"]
        if publication["state"] != "reserved" or signed["method"] != "POST":
            raise ValueError("unexpected publication state")
        fields = signed["fields"]
        if not isinstance(fields, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                 for k, v in fields.items()):
            raise ValueError("invalid upload fields")
        checksum = base64.b64encode(bytes.fromhex(metadata["sha256"])).decode("ascii")
        key = fields.get("key", "")
        pattern = (
            rf"v1/reviews/[1-9][0-9]*/{metadata['pull_number']}/{metadata['head_sha']}/bundles/"
            rf"([0-9a-f-]{{36}})/{metadata['revision']}/{metadata['sha256']}\.zip"
        )
        match = re.fullmatch(pattern, key)
        if not match or UUID(match.group(1)).int == 0:
            raise ValueError("unexpected publication object key")
        if (fields.get("Content-Type") != "application/zip"
                or fields.get("x-amz-checksum-algorithm") != "SHA256"
                or fields.get("x-amz-checksum-sha256") != checksum):
            raise ValueError("unexpected publication checksum policy")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise RuntimeError("Pair returned an invalid publication upload") from error
    return publication


def _finalize(server: str, token: str, publication: dict, metadata: dict) -> None:
    result = _request_json(
        server, token, f"/api/v1/reviews/publications/{publication['publication_id']}/finalize", {}, {200},
    )
    _validate_publication(result, metadata)
    if (result.get("publication_id") != publication["publication_id"]
            or result.get("review_id") != publication["review_id"] or result.get("state") != "finalized"
            or not isinstance(result.get("finalized_at"), str) or not result["finalized_at"]
            or type(result.get("content_version")) is not int or result["content_version"] < 1):
        raise RuntimeError("Pair did not confirm finalized review evidence")


def _multipart_parts(fields: dict[str, str], boundary: str, filename: str) -> tuple[list[bytes], bytes, bytes]:
    if not re.fullmatch(r"[0-9a-f]{40}\.zip", filename):
        raise ValueError("PR artifact ZIP filename must be a commit hash")
    parts = []
    for name, value in fields.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or "\r" in value or "\n" in value:
            raise ValueError("Pair BE returned an invalid S3 field")
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    file_header = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode()
    return parts, file_header, f"\r\n--{boundary}--\r\n".encode()


def _post_to_s3(signed: dict, archive: Path) -> None:
    parsed = urlparse(signed["url"])
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.port not in {None, 443} or parsed.username or parsed.password
            or not host.endswith(".amazonaws.com") or not re.search(r"(^|\.)s3[.-]", host)):
        raise ValueError("Pair BE returned a non-S3 HTTPS upload URL")
    fields = signed["fields"]
    boundary = "pair-" + secrets.token_hex(16)
    parts, file_header, closing = _multipart_parts(fields, boundary, archive.name)
    size = archive.stat().st_size
    if size < 1 or size > MAX_ARCHIVE_BYTES:
        raise ValueError("PR artifact ZIP exceeds the 100 MiB upload limit")
    content_length = sum(map(len, parts)) + len(file_header) + size + len(closing)
    if content_length > MAX_ARCHIVE_BYTES:
        raise ValueError("PR artifact multipart upload exceeds the 100 MiB limit")

    def body():
        yield from parts
        yield file_header
        with archive.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk
        yield closing

    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    connection = http.client.HTTPSConnection(
        host,
        port=parsed.port,
        timeout=UPLOAD_TIMEOUT_SECONDS,
        context=compatible_tls_context(),
    )
    try:
        connection.request("POST", target, body=body(), headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(content_length),
        })
        response = connection.getresponse()
        response.read(MAX_RESPONSE_BYTES)
        if response.status not in {200, 201, 204}:
            raise RuntimeError(f"S3 rejected PR artifact ZIP (HTTP {response.status})")
    finally:
        connection.close()


def upload_archive(pr_url: str, archive: Path, auth_path: Path) -> str:
    """Reserve, upload and finalize; an S3 success alone is not publication."""
    owner, repo, number = _file_path(pr_url).split("/")
    head = _commit_hash(archive)
    token, server = _upload_target(auth_path)
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("PR artifact ZIP must be a regular file")
    if not 0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError("PR artifact ZIP exceeds the 100 MiB upload limit")
    # Hold immutable bytes across hashing and upload while another scan can
    # atomically replace the original archive. Credentials never enter it.
    with tempfile.TemporaryDirectory(prefix="pair-publication-") as directory:
        snapshot = Path(directory) / archive.name
        shutil.copyfile(archive, snapshot)
        size = snapshot.stat().st_size
        if not 0 < size <= MAX_ARCHIVE_BYTES:
            raise ValueError("PR artifact ZIP exceeds the 100 MiB upload limit")
        digest = hashlib.sha256()
        with snapshot.open("rb") as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
        metadata = {"owner": owner, "repo": repo, "pull_number": int(number),
                    "head_sha": head, "sha256": digest.hexdigest(), "size": size}
        metadata["revision"] = _revision(archive, server, metadata)
        publication = _reserve(server, token, metadata)
        if publication["state"] == "reserved":
            _post_to_s3(publication["upload"], snapshot)
        _finalize(server, token, publication, metadata)
        return publication["publication_id"]
