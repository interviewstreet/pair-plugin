"""Upload one commit-scoped Pair ZIP through Pair BE's signed S3 POST."""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
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
    if not parsed.netloc or parsed.username or parsed.password or parsed.path not in {"", "/"}:
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
    return token, _server_origin(server)


def _upload_target(auth_path: Path) -> tuple[str | None, str]:
    if upload_server := os.environ.get("PAIRCLI_UPLOAD_SERVER_URL"):
        return None, _server_origin(upload_server)
    return _credential(auth_path)


def _sign_upload(server: str, token: str | None, file_path: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{server}/api/v1/upload/url",
        data=json.dumps({"filePath": file_path}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        opener = build_opener(NoRedirects(), HTTPSHandler(context=compatible_tls_context()))
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status != 201:
                raise RuntimeError(f"Pair BE rejected upload signing (HTTP {response.status})")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        if error.code == 401:
            raise RuntimeError("Pair CLI login expired or was rejected; run paircli login and retry upload") from error
        if error.code == 403:
            raise RuntimeError("Pair refused artifact upload for this account (HTTP 403)") from error
        raise RuntimeError(f"Pair BE rejected upload signing (HTTP {error.code})") from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Pair BE upload response is too large")
    try:
        signed = json.loads(raw)["data"]
        if signed["method"] != "POST" or signed["filePath"] != file_path:
            raise ValueError("unexpected signed upload")
        artifact_id = UUID(signed["id"])
        fields = signed["fields"]
        if not isinstance(fields, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in fields.items()
        ):
            raise ValueError("invalid S3 fields")
        if fields.get("Content-Type") != "application/zip" or fields.get("key") != f"repositories/{file_path}/{artifact_id}.zip":
            raise ValueError("unexpected S3 upload policy")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Pair BE returned an invalid signed upload") from error
    return signed


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
    """Sign with Pair BE, then stream the ZIP to S3."""
    file_path = _file_path(pr_url)
    token, server = _upload_target(auth_path)
    signed = _sign_upload(server, token, file_path)
    _post_to_s3(signed, archive)
    return signed["id"]
