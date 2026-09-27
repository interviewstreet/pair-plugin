#!/usr/bin/env python3
"""Open a review against a locally running pair-be instance."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from http.cookies import SimpleCookie
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_SIGNING_KEY = "local-only-internal-hs256-signing-key-not-for-production-01"
ORIGIN = "http://localhost:3000"
JWT_TTL = 5 * 60
REQUEST_TIMEOUT = 30.0
TERMINAL_FAILURES = {"failed", "discarded", "error"}


class OpenReviewError(RuntimeError):
    """A safe, user-facing failure that does not include credentials."""


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def mint_identity_jwt(
    *,
    now: Callable[[], float] = time.time,
    signing_key: str | None = None,
) -> str:
    """Create the short-lived identity token expected by pair-be."""
    key = signing_key if signing_key is not None else os.getenv(
        "PAIR_INTERNAL_AUTH_JWT_SIGNING_KEY", DEFAULT_SIGNING_KEY
    )
    key_bytes = key.encode("utf-8")
    if len(key_bytes) < 32:
        raise OpenReviewError("PAIR_INTERNAL_AUTH_JWT_SIGNING_KEY must be at least 32 bytes")

    issued_at = int(now())
    claims = {
        "sub": "pair-e2e-harness",
        "username": "pair-e2e",
        "email": "pair-e2e@localhost",
        "iss": "community",
        "aud": "pair",
        "iat": issued_at,
        "exp": issued_at + JWT_TTL,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    encoded_header = _b64url(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _b64url(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = hmac.new(key_bytes, signing_input, hashlib.sha256).digest()
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


def _request(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    cookie: str | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    headers = {"Accept": "application/json", "Origin": ORIGIN}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=max(0.1, timeout)) as response:
            raw = response.read()
            status = response.status
            response_headers = dict(response.headers.items())
    except HTTPError as exc:
        raise OpenReviewError(f"{method} {url.rsplit('/', 1)[-1]} returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise OpenReviewError(f"{method} request failed: {exc.__class__.__name__}") from None
    try:
        decoded = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenReviewError(f"{method} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise OpenReviewError(f"{method} returned an unexpected JSON body")
    return status, response_headers, decoded


def _session_cookie(headers: dict[str, str]) -> str:
    raw = headers.get("Set-Cookie", "")
    cookies = SimpleCookie()
    cookies.load(raw)
    for name in ("pair_session", "__Host-pair_session"):
        if name in cookies:
            return f"{name}={cookies[name].value}"
    raise OpenReviewError("exchange response did not set pair_session")


def _require_status(actual: int, expected: int, operation: str) -> None:
    if actual != expected:
        raise OpenReviewError(f"{operation} returned HTTP {actual}, expected {expected}")


def open_review(
    review_key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 600,
    interval: float = 5,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    signing_key: str | None = None,
) -> dict[str, Any]:
    """Exchange identity, create a review, and wait for its workspace."""
    if timeout < 0 or interval < 0:
        raise OpenReviewError("timeout and interval must be non-negative")
    base = base_url.rstrip("/")
    token = mint_identity_jwt(now=now, signing_key=signing_key)

    status, headers, exchange = _request(
        "POST",
        f"{base}/api/v1/auth/exchange",
        body={"identity_token": token},
    )
    _require_status(status, 201, "exchange")
    cookie = _session_cookie(headers)
    account = exchange.get("account")
    account_id = account.get("id") if isinstance(account, dict) else None
    if not isinstance(account_id, str) or not account_id:
        raise OpenReviewError("exchange response did not include account.id")

    status, _, created = _request(
        "POST",
        f"{base}/api/v1/accounts/{account_id}/reviews",
        body={"review_key": review_key},
        cookie=cookie,
    )
    _require_status(status, 201, "review creation")
    project = created.get("project")
    project_id = project.get("id") if isinstance(project, dict) else None
    if not isinstance(project_id, str) or not project_id:
        raise OpenReviewError("review response did not include project.id")

    deadline = now() + timeout
    while True:
        remaining = deadline - now()
        if remaining < 0:
            raise OpenReviewError(f"timed out after {timeout:g} seconds")
        status, _, project_body = _request(
            "GET",
            f"{base}/api/v1/accounts/{account_id}/projects/{project_id}",
            cookie=cookie,
            timeout=min(REQUEST_TIMEOUT, max(0.1, remaining)),
        )
        _require_status(status, 200, "project poll")
        workspace = project_body.get("workspace")
        workspace_status = workspace.get("status") if isinstance(workspace, dict) else None
        if workspace_status == "ready":
            result = {
                "project_id": project_id,
                "workspace_status": workspace_status,
                "remote_url": workspace.get("remote_url", ""),
                "review_key": review_key,
            }
            print(f"project id: {result['project_id']}")
            print(f"workspace status: {result['workspace_status']}")
            print(f"remote_url: {result['remote_url']}")
            print(f"review_key: {result['review_key']}")
            return result
        if workspace_status in TERMINAL_FAILURES:
            raise OpenReviewError(f"workspace reached terminal status: {workspace_status}")
        if remaining == 0:
            raise OpenReviewError(f"timed out after {timeout:g} seconds")
        sleep(min(interval, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_key")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args(argv)
    try:
        open_review(
            args.review_key,
            base_url=args.base_url,
            timeout=args.timeout,
            interval=args.interval,
        )
    except OpenReviewError as exc:
        print(f"open_review: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
