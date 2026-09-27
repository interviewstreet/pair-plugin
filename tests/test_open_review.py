import base64
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "open_review.py"
SPEC = importlib.util.spec_from_file_location("open_review", SCRIPT)
open_review = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(open_review)


def decode_claims(token):
    encoded = token.split(".")[1]
    return json.loads(
        base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    )


class FakePair:
    def __init__(self, statuses=("ready",), review_status=201):
        self.statuses = list(statuses)
        self.review_status = review_status
        self.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append((self.command, self.path, self.headers, body))
                if self.path.endswith("/auth/exchange"):
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie", "pair_session=session-value; Path=/")
                    self.end_headers()
                    self.wfile.write(
                        b'{"user":{"id":"u"},"account":{"id":"account-1"},'
                        b'"membership":{"id":"m"}}'
                    )
                elif self.path.endswith("/reviews"):
                    self.send_response(fake.review_status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    if fake.review_status == 201:
                        self.wfile.write(
                            b'{"project":{"id":"project-1"},"repositories":[],'
                            b'"workspace":{"status":"queued"},"review":{}}'
                        )
                    else:
                        self.wfile.write(b'{"error":"conflict"}')

            def do_GET(self):
                fake.requests.append((self.command, self.path, self.headers, None))
                status = fake.statuses.pop(0) if len(fake.statuses) > 1 else fake.statuses[0]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                payload = {
                    "project": {"id": "project-1"},
                    "repositories": [],
                    "workspace": {
                        "status": status,
                        "remote_url": "http://workspace.local",
                    },
                }
                self.wfile.write(json.dumps(payload).encode())

        return Handler

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def fake():
    value = FakePair()
    yield value
    value.close()


def test_jwt_claims_have_five_minute_ttl_without_exposing_key():
    key = "test-signing-key-that-is-at-least-32-bytes"
    token = open_review.mint_identity_jwt(now=lambda: 1_700_000_000, signing_key=key)
    claims = decode_claims(token)
    assert claims == {
        "sub": "pair-e2e-harness",
        "username": "pair-e2e",
        "email": "pair-e2e@localhost",
        "iss": "community",
        "aud": "pair",
        "iat": 1_700_000_000,
        "exp": 1_700_000_300,
    }
    assert key not in token


def test_exchange_review_and_poll_until_ready(fake, capsys):
    result = open_review.open_review(
        "acme__repo__1__abcdef012345",
        base_url=fake.url,
        interval=0,
        now=lambda: 1_700_000_000,
        signing_key="test-signing-key-that-is-at-least-32-bytes",
    )
    assert result["workspace_status"] == "ready"
    assert [request[0:2] for request in fake.requests] == [
        ("POST", "/api/v1/auth/exchange"),
        ("POST", "/api/v1/accounts/account-1/reviews"),
        ("GET", "/api/v1/accounts/account-1/projects/project-1"),
    ]
    assert fake.requests[1][2]["Cookie"] == "pair_session=session-value"
    assert fake.requests[1][2]["Origin"] == "http://localhost:3000"
    assert "remote_url: http://workspace.local" in capsys.readouterr().out


def test_non_ready_status_later_becomes_ready(fake):
    fake.statuses = ["queued", "provisioning", "ready"]
    assert open_review.open_review(
        "key",
        base_url=fake.url,
        interval=0,
        now=lambda: 1_700_000_000,
        signing_key="test-signing-key-that-is-at-least-32-bytes",
    )["workspace_status"] == "ready"
    assert len([request for request in fake.requests if request[0] == "GET"]) == 3


def test_failed_workspace_is_terminal(fake):
    fake.statuses = ["failed"]
    with pytest.raises(open_review.OpenReviewError, match="failed"):
        open_review.open_review(
            "key",
            base_url=fake.url,
            interval=0,
            now=lambda: 1_700_000_000,
            signing_key="test-signing-key-that-is-at-least-32-bytes",
        )


def test_poll_timeout(fake):
    fake.statuses = ["queued"]
    current = [100.0]

    def now():
        return current[0]

    def advance(seconds):
        current[0] += seconds

    with pytest.raises(open_review.OpenReviewError, match="timed out"):
        open_review.open_review(
            "key",
            base_url=fake.url,
            timeout=2,
            interval=1,
            now=now,
            sleep=advance,
            signing_key="test-signing-key-that-is-at-least-32-bytes",
        )


def test_review_conflict_is_reported(fake):
    fake.review_status = 409
    with pytest.raises(open_review.OpenReviewError, match="HTTP 409"):
        open_review.open_review(
            "key",
            base_url=fake.url,
            now=lambda: 1_700_000_000,
            signing_key="test-signing-key-that-is-at-least-32-bytes",
        )
