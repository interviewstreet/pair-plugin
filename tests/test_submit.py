"""Unit tests for the submit stream: key grammar, payload contents, HTTP shape.

Nothing here touches the network. `submit.submit` and `submit.status` take an `opener`
callable, and the tests substitute a fake that captures the request and returns a canned
response. That keeps every test hermetic and fast.
"""
from __future__ import annotations

import io
import json
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import pair.brief as brief_mod
from pair.brief import prepare
from pair.config import Config, load_config
from pair.submit import (SubmitError, build_meta, build_payload, resolve_status_key, status, submit)
from pair.util import is_submit_key, parse_submit_key, submit_key


# ---------------------------------------------------------------- key grammar

def test_submit_key_roundtrip():
    k = submit_key("interviewstreet/pair-be", 62, "3453326DA3791b0f4c8e2d5a6079bb31e4c7f8a2")
    assert k == "interviewstreet__pair-be__62__3453326da379"
    repo, n, sha = parse_submit_key(k)
    assert repo == "interviewstreet/pair-be" and n == 62 and sha == "3453326da379"
    assert is_submit_key(k)


def test_submit_key_allows_single_underscore_in_names():
    # Owner and repo may each contain a single underscore. Split-on-`__` still yields 4 parts.
    k = submit_key("some_owner/some_repo", 3, "abcdef123456")
    assert k == "some_owner__some_repo__3__abcdef123456"
    assert parse_submit_key(k) == ("some_owner/some_repo", 3, "abcdef123456")


def test_submit_key_rejects_double_underscore_in_names():
    with pytest.raises(ValueError, match="key-safe"):
        submit_key("bad__owner/repo", 1, "abcdef123456")
    with pytest.raises(ValueError, match="key-safe"):
        submit_key("owner/bad__repo", 1, "abcdef123456")


def test_submit_key_needs_full_owner_repo_and_hex_sha():
    with pytest.raises(ValueError, match="owner/name"):
        submit_key("noslash", 1, "abcdef123456")
    with pytest.raises(ValueError, match="head_sha"):
        submit_key("o/r", 1, "not-hex")
    with pytest.raises(ValueError, match="positive"):
        submit_key("o/r", 0, "abcdef123456")


def test_parse_submit_key_rejects_wrong_shape():
    # Wrong number of `__` chunks, bad chars, short SHAs, non-numeric PR.
    for bad in ("only__three__62", "o__r__62__abcdef123456__extra", "o__r__x__abcdef123456",
                "o__r__62__ABCDEF123456", "o__r__62__abcdef1234", ""):
        assert not is_submit_key(bad)
        with pytest.raises(ValueError):
            parse_submit_key(bad)


# ---------------------------------------------------------------- payload contents

@pytest.fixture
def prepared(demo, pr7, monkeypatch):
    """Prepare a real brief in a temp home so the payload has concrete files to pack."""
    monkeypatch.setattr(brief_mod, "fetch_pr", lambda repo, n, cfg: pr7)
    out = prepare("acme/demo#7", load_config())
    return out


def _members(gz: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(gz), mode="r:gz") as tf:
        return list(tf.getmembers())


def _read(gz: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(gz), mode="r:gz") as tf:
        return tf.extractfile(name).read()


PR7_HEAD = "aaaaaaaabbbbbbbbccccccccdddddddd11112222"
PR7_KEY = "acme__demo__7__aaaaaaaabbbb"  # first 12 chars of PR7_HEAD


def test_build_meta_carries_the_six_identifying_fields(prepared):
    meta = build_meta(prepared)
    assert set(meta) == {"key", "repo", "pr", "title", "base_sha", "head_sha"}
    assert meta["repo"] == "acme/demo" and meta["pr"] == 7
    assert meta["head_sha"] == PR7_HEAD
    assert len(meta["base_sha"]) == 40
    assert meta["key"] == PR7_KEY


def test_build_meta_orders_the_fields_as_the_contract_lists_them(prepared):
    # The service rejects anything but these six, in this shape.
    assert list(build_meta(prepared)) == ["key", "repo", "pr", "title", "base_sha", "head_sha"]


def test_build_meta_base_sha_is_the_merge_base_not_the_base_branch_tip(prepared, pr7):
    meta = build_meta(prepared)
    assert meta["base_sha"] == pr7.merge_base
    assert meta["base_sha"] != pr7.base_oid


@pytest.mark.parametrize("field", ["head_sha", "merge_base"])
@pytest.mark.parametrize("bad", ["", "aaaaaaaabbbb", "AAAAAAAABBBBBBBBCCCCCCCCDDDDDDDD11112222"])
def test_build_meta_rejects_shas_that_are_not_40_lowercase_hex(prepared, field, bad):
    """Fail on the builder rather than ship a payload /submit has to reject."""
    # The top-level head_sha is the fallback build_meta reads, so break both.
    broken = {**prepared, "head_sha": bad, "pr_basics": {**prepared["pr_basics"], field: bad}}
    with pytest.raises(SubmitError, match="40 lowercase hex"):
        build_meta(broken)


def test_build_payload_bundles_meta_brief_and_sessions(prepared):
    gz, meta = build_payload(prepared)
    names = [m.name for m in _members(gz)]
    assert names[0] == "meta.json"
    assert "brief.json" in names and "evidence.json" in names
    assert "diff.patch" in names and "risky_hunks.json" in names
    # Session JSONL from both agents is packed under the sessions/ prefix.
    session_names = [n for n in names if n.startswith("sessions/")]
    assert session_names, "expected at least one raw session file in the payload"
    assert any(n.startswith("sessions/claude/") for n in session_names)
    assert any(n.startswith("sessions/codex/") for n in session_names)
    # meta.json is exactly what build_meta produced.
    assert json.loads(_read(gz, "meta.json").decode()) == meta
    # Brief content is copied verbatim.
    on_disk = (Path(prepared["dir"]) / "brief.json").read_bytes()
    assert _read(gz, "brief.json") == on_disk


def test_build_payload_respects_archive_safety(prepared):
    gz, _ = build_payload(prepared)
    for m in _members(gz):
        assert m.isreg(), f"non-regular file in payload: {m.name} ({m.type!r})"
        assert not m.issym() and not m.islnk()
        assert not m.name.startswith("/"), f"absolute path in payload: {m.name}"
        assert ".." not in m.name.split("/"), f"parent traversal in payload: {m.name}"
        assert "\\" not in m.name, f"backslash in payload: {m.name}"


def test_build_payload_uniquifies_colliding_session_basenames(prepared, tmp_path):
    """Two sessions with identical basenames land under distinct arcnames."""
    a = tmp_path / "one" / "same.jsonl"
    b = tmp_path / "two" / "same.jsonl"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    a.write_bytes(b"A\n")
    b.write_bytes(b"B\n")
    # Point both files outside claude/codex roots so they land under sessions/other/.
    prepared_extra = {**prepared, "session_files": [str(a), str(b)]}
    gz, _ = build_payload(prepared_extra, claude_root=tmp_path / "no-claude", codex_root=tmp_path / "no-codex")
    names = [m.name for m in _members(gz)]
    others = [n for n in names if n.startswith("sessions/other/")]
    assert others == ["sessions/other/same.jsonl", "sessions/other/same-2.jsonl"]
    # And the payload still passes archive safety.
    for m in _members(gz):
        assert m.isreg() and not m.name.startswith("/") and ".." not in m.name.split("/")


def test_build_payload_skips_missing_session_files(prepared, tmp_path):
    ghost = tmp_path / "gone.jsonl"  # never created
    prepared_extra = {**prepared, "session_files": [str(ghost)]}
    gz, _ = build_payload(prepared_extra)
    names = [m.name for m in _members(gz)]
    assert not any(n.startswith("sessions/other/gone") for n in names)


# ---------------------------------------------------------------- HTTP

class _FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingOpener:
    """A stand-in for urllib.request.urlopen. Records every request; replays canned responses."""

    def __init__(self, responses: list[tuple[int, dict] | urllib.error.HTTPError]):
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []
        self.bodies: list[bytes] = []

    def __call__(self, req: urllib.request.Request):
        self.requests.append(req)
        # data is available as `req.data`; capture bytes even when None.
        self.bodies.append(req.data or b"")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        code, body = resp
        return _FakeResponse(code, body)


def _cfg(**overrides) -> Config:
    c = Config()
    c.service_url = overrides.get("service_url", "https://pair.example.test")
    c.service_token = overrides.get("service_token", "tok-XYZ")
    return c


def test_submit_posts_gzip_with_bearer_and_force_query(prepared):
    opener = _RecordingOpener([(202, {"key": PR7_KEY})])
    result = submit("acme/demo#7", _cfg(), force=True,
                    prepare_fn=lambda spec, cfg, force: prepared, opener=opener)
    req = opener.requests[0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://pair.example.test/submit?force=true"
    assert req.get_header("Content-type") == "application/gzip"
    assert req.get_header("Authorization") == "Bearer tok-XYZ"
    # The body is a valid gzip'd tar that contains meta.json.
    with tarfile.open(fileobj=io.BytesIO(opener.bodies[0]), mode="r:gz") as tf:
        meta = json.loads(tf.extractfile("meta.json").read())
    assert meta["key"] == PR7_KEY
    assert result["status"] == "queued" and result["started"] is True
    assert result["http_status"] == 202 and result["force"] is True
    assert result["key"] == PR7_KEY
    assert result["payload_bytes"] == len(opener.bodies[0])


def test_submit_force_defaults_to_false(prepared):
    opener = _RecordingOpener([(200, {"key": PR7_KEY, "status": "done"})])
    result = submit("acme/demo#7", _cfg(),
                    prepare_fn=lambda spec, cfg, force: prepared, opener=opener)
    assert opener.requests[0].full_url.endswith("?force=false")
    assert result["status"] == "done" and result["started"] is False
    assert result["http_status"] == 200


def test_submit_requires_service_url_and_token(prepared):
    with pytest.raises(SubmitError, match="service_url"):
        submit("acme/demo#7", _cfg(service_url=""),
               prepare_fn=lambda spec, cfg, force: prepared, opener=_RecordingOpener([]))
    with pytest.raises(SubmitError, match="service_token"):
        submit("acme/demo#7", _cfg(service_token=""),
               prepare_fn=lambda spec, cfg, force: prepared, opener=_RecordingOpener([]))


def test_submit_raises_on_http_error(prepared):
    err = urllib.error.HTTPError("https://pair.example.test/submit?force=false", 401, "Unauthorized",
                                 {}, io.BytesIO(b'{"error":"bad token"}'))
    opener = _RecordingOpener([err])
    with pytest.raises(SubmitError, match="HTTP 401"):
        submit("acme/demo#7", _cfg(),
               prepare_fn=lambda spec, cfg, force: prepared, opener=opener)


def test_submit_never_starts_the_local_server_or_calls_claude(prepared, monkeypatch):
    """The builder's job ends at the POST.

    Before PoC2, building a brief started `pair.server` on the laptop and ran
    the writers there through the Claude CLI. Both are process spawns, so
    banning `subprocess` for the duration of the call covers them together,
    and `ensure_server` is stubbed as well in case it ever stops shelling out.
    """
    import subprocess

    import pair.server

    def boom(*args, **kwargs):
        raise AssertionError(f"submit spawned a process: {args!r}")

    monkeypatch.setattr(pair.server, "ensure_server", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)

    opener = _RecordingOpener([(202, {"key": PR7_KEY})])
    result = submit("acme/demo#7", _cfg(),
                    prepare_fn=lambda spec, cfg, force: prepared, opener=opener)
    assert result["status"] == "queued"
    # One request, and nothing in the reply points at a local server.
    assert len(opener.requests) == 1
    assert "server" not in result and "url" not in result


def test_submit_propagates_prepare_error():
    with pytest.raises(SubmitError, match="prepare failed: boom"):
        submit("acme/demo#7", _cfg(),
               prepare_fn=lambda spec, cfg, force: {"status": "error", "error": "prepare failed: boom"},
               opener=_RecordingOpener([]))


SAMPLE_KEY = "acme__demo__7__abcdef123456"


def test_status_get_uses_bearer_and_returns_manifest():
    body = {"manifest": {"status": "running", "key": SAMPLE_KEY}, "bundle_url": None}
    opener = _RecordingOpener([(200, body)])
    result = status(SAMPLE_KEY, _cfg(), opener=opener)
    req = opener.requests[0]
    assert req.get_method() == "GET"
    assert req.full_url == f"https://pair.example.test/reviews/{SAMPLE_KEY}"
    assert req.get_header("Authorization") == "Bearer tok-XYZ"
    assert result["status"] == "running" and result["manifest"] == body["manifest"]
    assert result["bundle_url"] is None


def test_status_404_returns_unknown():
    err = urllib.error.HTTPError("https://pair.example.test/reviews/x", 404, "Not Found",
                                 {}, io.BytesIO(b""))
    opener = _RecordingOpener([err])
    result = status(SAMPLE_KEY, _cfg(), opener=opener)
    assert result["status"] == "unknown" and result["http_status"] == 404


def test_status_401_raises():
    err = urllib.error.HTTPError("https://pair.example.test/reviews/x", 401, "Unauthorized",
                                 {}, io.BytesIO(b""))
    with pytest.raises(SubmitError, match="401"):
        status(SAMPLE_KEY, _cfg(), opener=_RecordingOpener([err]))


def test_resolve_status_key_from_repo_spec(prepared):
    # prepared() wrote a local brief dir under briefs_dir(); resolve_status_key finds it.
    key = resolve_status_key("acme/demo#7", load_config())
    assert key == PR7_KEY


def test_resolve_status_key_errors_when_no_local_brief(demo):
    # `demo` fixture sets PAIR_HOME to a temp path with no briefs written yet.
    with pytest.raises(SubmitError, match="no local brief"):
        resolve_status_key("acme/demo#7", load_config())


def test_resolve_status_key_passes_through_full_key(demo):
    assert resolve_status_key(SAMPLE_KEY, load_config()) == SAMPLE_KEY
