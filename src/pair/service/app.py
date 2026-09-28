"""FastAPI app for the review service.

Wires the HTTP surface described in ``docs/bundle.md`` to the job
manager. Everything is behind bearer auth with a constant-time compare, and
the app never talks to Claude or GCS directly — those go through the
injected ``ClaudeRunner`` and ``Storage`` implementations.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from pair.service.archive import (
    ArchiveError,
    MAX_COMPRESSED_BYTES,
    extract_safe,
)
from pair.service.claude import LoginCheck
from pair.service.jobs import JobManager
from pair.service.keys import BadKey, parse_key
from pair.service.storage import bundle_path, gce_access_token, manifest_path

log = logging.getLogger("pair.service.app")

SIGNED_URL_TTL_SECONDS = 15 * 60

# Meta.json is a tiny JSON blob; cap the peek so a hostile 200 MiB "meta.json"
# can't slow the request handler down.
_META_MAX_BYTES = 128 * 1024


@dataclass
class ServiceConfig:
    bearer_token: str
    signed_url_ttl_seconds: int = SIGNED_URL_TTL_SECONDS


# --------------------------------------------------------------------------- #
#  Bearer auth
# --------------------------------------------------------------------------- #


def _unauthorized(msg: str) -> HTTPException:
    return HTTPException(status_code=401, detail={"error": msg})


def _check_bearer(expected: str, header_value: str | None) -> None:
    """Constant-time bearer compare. Missing/malformed/wrong all yield 401."""
    if not header_value:
        raise _unauthorized("missing Authorization header")
    parts = header_value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise _unauthorized("malformed Authorization header")
    presented = parts[1]
    if not secrets.compare_digest(presented, expected):
        raise _unauthorized("bad bearer token")


# --------------------------------------------------------------------------- #
#  Meta.json peek
# --------------------------------------------------------------------------- #


def _peek_meta(work: Path) -> dict:
    """Read meta.json from an already-extracted payload work dir."""
    meta_path = work / "meta.json"
    if not meta_path.exists():
        raise ArchiveError("payload is missing meta.json")
    raw = meta_path.read_bytes()
    if len(raw) > _META_MAX_BYTES:
        raise ArchiveError("meta.json exceeds 128 KiB")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArchiveError(f"meta.json is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ArchiveError("meta.json must be a JSON object")
    return obj


# The contract fixes meta.json at exactly these fields, in this order. An
# extra field means the plugin and the service disagree about the payload, and
# the manifest we mint from it would be a guess, so the submit is rejected.
_REQUIRED_META = ("key", "repo", "pr", "title", "base_sha", "head_sha")

_SHA40 = re.compile(r"^[0-9a-f]{40}$")


def _validate_meta(meta: dict) -> tuple[str, dict]:
    missing = [f for f in _REQUIRED_META if f not in meta]
    if missing:
        raise ArchiveError(f"meta.json is missing fields: {', '.join(missing)}")
    extra = [f for f in meta if f not in _REQUIRED_META]
    if extra:
        raise ArchiveError(f"meta.json has unexpected fields: {', '.join(sorted(extra))}")
    key = meta["key"]
    try:
        parsed = parse_key(key)
    except BadKey as exc:
        raise ArchiveError(f"meta.json.key: {exc}") from exc
    if meta["repo"] != f"{parsed.owner}/{parsed.repo}":
        raise ArchiveError("meta.json.repo does not match key")
    if int(meta["pr"]) != parsed.pr:
        raise ArchiveError("meta.json.pr does not match key")
    # Both SHAs land in manifest.json verbatim, where the contract requires 40
    # lowercase hex, so they are checked here rather than shipped onward.
    head_sha = str(meta["head_sha"])
    if not _SHA40.match(head_sha):
        raise ArchiveError("meta.json.head_sha must be 40 lowercase hex characters")
    if not _SHA40.match(str(meta["base_sha"])):
        raise ArchiveError("meta.json.base_sha must be 40 lowercase hex characters")
    if not head_sha.startswith(parsed.head12):
        raise ArchiveError("meta.json.head_sha does not start with the key's head12")
    return key, {
        "key": key,
        "repo": meta["repo"],
        "pr": int(meta["pr"]),
        "title": str(meta["title"]),
        "base_sha": str(meta["base_sha"]),
        "head_sha": head_sha,
    }


# --------------------------------------------------------------------------- #
#  App factory
# --------------------------------------------------------------------------- #


def create_app(
    *,
    manager: JobManager,
    config: ServiceConfig,
    login_check: LoginCheck | Callable[[], bool] | None = None,
    access_token: Callable[[], str] | None = None,
) -> FastAPI:
    app = FastAPI(title="pair-review", version="0.1.0")
    login = login_check or manager.login_check
    token = config.bearer_token
    mint_bundle_token = access_token or gce_access_token

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        # Uniform envelope: bodies are always JSON objects, never bare strings.
        detail = exc.detail
        if not isinstance(detail, dict):
            detail = {"error": str(detail)}
        return JSONResponse(status_code=exc.status_code, content=detail)

    @app.get("/health")
    async def health(authorization: str | None = Header(default=None)):
        _check_bearer(token, authorization)
        try:
            ok = bool(login())
        except Exception:
            ok = False
        if ok:
            return {"status": "ok"}
        raise HTTPException(status_code=503, detail={"error": "claude not logged in"})

    @app.post("/submit")
    async def submit(
        request: Request,
        force: bool = Query(default=False),
        authorization: str | None = Header(default=None),
        content_type: str | None = Header(default=None, alias="Content-Type"),
    ):
        _check_bearer(token, authorization)
        if not content_type or content_type.split(";")[0].strip().lower() != "application/gzip":
            raise HTTPException(
                status_code=415,
                detail={"error": "Content-Type must be application/gzip"},
            )
        body = await request.body()
        if len(body) > MAX_COMPRESSED_BYTES:
            raise HTTPException(
                status_code=413,
                detail={"error": f"payload exceeds {MAX_COMPRESSED_BYTES} bytes"},
            )
        # Full safe-extract to a temp dir purely to validate + peek meta.json.
        # The job re-extracts from the stored bytes for isolation.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="pair-submit-") as tmp:
            work = Path(tmp)
            try:
                extract_safe(body, work)
                meta_raw = _peek_meta(work)
                key, meta = _validate_meta(meta_raw)
            except ArchiveError as exc:
                raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

        # Store the raw payload before acknowledging: even for the 200 (done)
        # branch we archive the submit so operators can replay it.
        try:
            payload_ref = manager.store_payload(key, body)
        except Exception as exc:  # pragma: no cover - storage error path
            log.exception("submit %s: failed to archive payload", key)
            raise HTTPException(
                status_code=502, detail={"error": f"payload archive failed: {exc}"}
            ) from exc

        # Duplicate + force logic.
        current = manager.status_for(key)
        if current == "done" and not force:
            return JSONResponse(
                status_code=200, content={"key": key, "status": "done"}
            )
        manager.start(key, meta, payload_ref=payload_ref)
        return JSONResponse(status_code=202, content={"key": key})

    @app.get("/reviews/{key}")
    async def get_review(key: str, authorization: str | None = Header(default=None)):
        _check_bearer(token, authorization)
        try:
            parse_key(key)
        except BadKey as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        mpath = manifest_path(key)
        if not manager.storage.exists(mpath):
            raise HTTPException(status_code=404, detail={"error": "unknown key"})
        try:
            manifest = json.loads(manager.storage.get_bytes(mpath).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=500, detail={"error": f"manifest is corrupt: {exc}"}
            ) from exc
        bundle_url = None
        bundle_token = None
        if manifest.get("status") == "done" and manager.storage.exists(bundle_path(key)):
            bundle_url = manager.storage.object_url(bundle_path(key))
            try:
                bundle_token = mint_bundle_token()
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail={"error": "bundle token unavailable"}
                ) from exc
            if not bundle_token:
                raise HTTPException(status_code=503, detail={"error": "bundle token unavailable"})
        return {"manifest": manifest, "bundle_url": bundle_url, "bundle_token": bundle_token}

    return app
