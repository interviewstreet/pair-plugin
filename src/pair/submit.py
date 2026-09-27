"""Build the review payload as a `tar.gz` and POST it to the review service.

The plugin's only job on submit is: run `prepare()` to make the local brief dir, pack the
brief outputs plus the raw session JSONL of every kept session (including subagent files)
into a `tar.gz`, and hand that blob to the service at `{service_url}/submit`. The service
owns everything downstream (queueing, rendering, bundling). We never start a local review
server from here, and we never call Claude from the plugin.

The archive follows the frozen bundle contract at `docs/bundle.md`:

* Members are regular files. No absolute paths, no `..`, no backslashes, no symlinks.
* `meta.json` carries `key`, `repo`, `pr`, `title`, `base_sha`, and `head_sha`.
* Session JSONL is namespaced under `sessions/<agent>/...` so subagent files and repeats
  never collide with each other, and never leak the caller's absolute paths.
"""
from __future__ import annotations

import io
import json
import re
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from pair.brief import latest_dir, prepare
from pair.config import Config, claude_dir, codex_dir
from pair.util import is_submit_key, parse_pr_spec, review_key, submit_key

# Files the prepare step writes into the brief directory. `brief.md` is listed for
# forward compatibility; today it isn't produced, and the archive just skips it.
BUNDLE_FILES = ("brief.json", "brief.md", "evidence.json", "diff.patch", "risky_hunks.json")

SHA40 = re.compile(r"^[0-9a-f]{40}$")


class SubmitError(Exception):
    """Raised when submit can't proceed (missing config, prepare failed, bad HTTP)."""


# ---------------------------------------------------------------- payload assembly

def _tarinfo(name: str, size: int, mtime: float) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mtime = int(mtime)
    info.mode = 0o644
    info.type = tarfile.REGTYPE  # explicit: never a symlink, hard link, device, or FIFO
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _add_bytes(tf: tarfile.TarFile, name: str, data: bytes, mtime: float) -> None:
    if not _is_safe_arcname(name):
        raise SubmitError(f"unsafe archive name: {name!r}")
    tf.addfile(_tarinfo(name, len(data), mtime), io.BytesIO(data))


def _is_safe_arcname(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name:
        return False
    parts = name.split("/")
    return all(p and p not in ("", ".", "..") for p in parts)


def _uniquify(name: str, used: set[str]) -> str:
    if name not in used:
        used.add(name)
        return name
    stem, _, ext = name.rpartition(".")
    root, dot = (stem, ".") if stem else (name, "")
    i = 2
    while (candidate := f"{root}-{i}{dot}{ext}") in used:
        i += 1
    used.add(candidate)
    return candidate


def _session_arcname(path: Path, claude_root: Path, codex_root: Path, used: set[str]) -> str:
    """Repo-safe archive name for one session JSONL. Falls back to `sessions/other/<basename>`
    when the file isn't under either agent's store, or when its relative name would be unsafe."""
    for agent, root in (("claude", claude_root), ("codex", codex_root)):
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        candidate = f"sessions/{agent}/{rel}"
        if _is_safe_arcname(candidate):
            return _uniquify(candidate, used)
    return _uniquify(f"sessions/other/{path.name}", used)


def build_meta(prepared: dict) -> dict:
    """The `meta.json` object: exactly the six identifying fields the service keys on.

    `base_sha` is the PR's merge base, not its base branch tip: the service copies
    both SHAs straight into `manifest.json`, where the contract requires 40 lowercase
    hex, so they are checked here instead of being rejected at `/submit`.
    """
    basics = prepared.get("pr_basics") or {}
    repo = basics.get("repo") or ""
    number = int(basics.get("number") or 0)
    head_sha = str(basics.get("head_sha") or prepared.get("head_sha") or "")
    base_sha = str(basics.get("merge_base") or "")
    for name, sha in (("head_sha", head_sha), ("base_sha", base_sha)):
        if not SHA40.match(sha):
            raise SubmitError(f"{name} must be 40 lowercase hex characters, got {sha!r}")
    return {
        "key": submit_key(repo, number, head_sha),
        "repo": repo,
        "pr": number,
        "title": str(basics.get("title") or ""),
        "base_sha": base_sha,
        "head_sha": head_sha,
    }


def build_payload(prepared: dict, *, claude_root: Path | None = None,
                  codex_root: Path | None = None) -> tuple[bytes, dict]:
    """Return `(gz_bytes, meta)`. Meta is echoed to the caller so it can log the key."""
    claude_root = claude_root or claude_dir()
    codex_root = codex_root or codex_dir()
    d = Path(prepared["dir"])
    session_files = list(prepared.get("session_files") or [])
    meta = build_meta(prepared)
    meta_bytes = json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8")

    buf = io.BytesIO()
    now = time.time()
    used: set[str] = set()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        _add_bytes(tf, "meta.json", meta_bytes, now)
        used.add("meta.json")
        for name in BUNDLE_FILES:
            path = d / name
            if not path.exists() or not path.is_file():
                continue
            data = path.read_bytes()
            _add_bytes(tf, name, data, path.stat().st_mtime)
            used.add(name)
        for raw in session_files:
            path = Path(raw)
            # Skip missing files silently: prepare() may have referenced cached paths that
            # have since rotated out of the reader stores.
            if not path.exists() or not path.is_file():
                continue
            arcname = _session_arcname(path, claude_root, codex_root, used)
            _add_bytes(tf, arcname, path.read_bytes(), path.stat().st_mtime)
    return buf.getvalue(), meta


# ---------------------------------------------------------------- HTTP

def _open_default(req: urllib.request.Request):  # pragma: no cover - trivial passthrough
    return urllib.request.urlopen(req, timeout=60)


Opener = Callable[[urllib.request.Request], object]


def _auth_header(token: str) -> str:
    if not token:
        raise SubmitError("no service_token configured (set service_token in config.toml)")
    return f"Bearer {token}"


def _post(url: str, token: str, body: bytes, force: bool, opener: Opener) -> tuple[int, dict]:
    q = "true" if force else "false"
    req = urllib.request.Request(f"{url.rstrip('/')}/submit?force={q}", data=body, method="POST")
    req.add_header("Content-Type", "application/gzip")
    req.add_header("Authorization", _auth_header(token))
    try:
        resp = opener(req)
    except urllib.error.HTTPError as e:
        raise SubmitError(f"HTTP {e.code} from {req.full_url}: "
                          f"{e.read().decode('utf-8', errors='replace')[:400]}") from e
    except urllib.error.URLError as e:
        raise SubmitError(f"cannot reach {req.full_url}: {e.reason}") from e
    with resp as r:
        payload = r.read().decode("utf-8") or "{}"
        return getattr(r, "status", 200), json.loads(payload)


def _get(url: str, token: str, path: str, opener: Opener) -> tuple[int, dict]:
    req = urllib.request.Request(f"{url.rstrip('/')}{path}", method="GET")
    req.add_header("Authorization", _auth_header(token))
    try:
        resp = opener(req)
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            return e.code, {"error": e.reason}
        raise SubmitError(f"HTTP {e.code}: "
                          f"{e.read().decode('utf-8', errors='replace')[:400]}") from e
    except urllib.error.URLError as e:
        raise SubmitError(f"cannot reach {req.full_url}: {e.reason}") from e
    with resp as r:
        payload = r.read().decode("utf-8") or "{}"
        return getattr(r, "status", 200), json.loads(payload)


# ---------------------------------------------------------------- public commands

def submit(spec: str, cfg: Config, *, force: bool = False,
           prepare_fn: Callable = prepare, opener: Opener = _open_default,
           claude_root: Path | None = None, codex_root: Path | None = None) -> dict:
    """Prepare the brief for `spec`, build the payload, POST it, return the service reply.

    Never touches the local review server. `prepare_fn` and `opener` are dependency
    injection seams for tests; production always uses the defaults.
    """
    if not cfg.service_url:
        raise SubmitError("no service_url configured (set service_url in config.toml)")
    prepared = prepare_fn(spec, cfg, force=force)
    if prepared.get("status") == "error":
        raise SubmitError(prepared.get("error") or "prepare failed")
    body, meta = build_payload(prepared, claude_root=claude_root, codex_root=codex_root)
    status_code, data = _post(cfg.service_url, cfg.service_token, body, force, opener)
    key = data.get("key") or meta["key"]
    result = {"key": key, "http_status": status_code, "payload_bytes": len(body),
              "force": force, "sessions": len(prepared.get("session_files") or [])}
    if status_code == 202:
        return {**result, "status": "queued", "started": True}
    if status_code == 200 and data.get("status") == "done":
        return {**result, "status": "done", "started": False}
    raise SubmitError(f"unexpected {status_code} from /submit: {data}")


def resolve_status_key(arg: str, cfg: Config) -> str:
    """Accept either a full submit key or `owner/repo#N`; for the latter, look up the
    most recent local brief dir to recover the head SHA."""
    if is_submit_key(arg):
        return arg
    repo, n = parse_pr_spec(arg)
    d = latest_dir(review_key(repo, n))
    if not d:
        raise SubmitError(f"no local brief for {repo}#{n}; submit it first or pass the full key")
    return submit_key(repo, n, d.name)


def status(arg: str, cfg: Config, *, opener: Opener = _open_default) -> dict:
    if not cfg.service_url:
        raise SubmitError("no service_url configured (set service_url in config.toml)")
    key = resolve_status_key(arg, cfg)
    code, data = _get(cfg.service_url, cfg.service_token, f"/reviews/{key}", opener)
    if code == 404:
        return {"key": key, "http_status": 404, "status": "unknown"}
    if code == 401:
        raise SubmitError(f"401 unauthorized from /reviews/{key}: bad or missing service_token")
    manifest = (data or {}).get("manifest") or {}
    return {"key": key, "http_status": code, "status": manifest.get("status") or "unknown",
            "manifest": manifest, "bundle_url": (data or {}).get("bundle_url")}
