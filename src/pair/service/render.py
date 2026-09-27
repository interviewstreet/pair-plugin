"""Render sessions and the brief markdown, and package the final bundle.

The bundle is what the VM downloads: no raw JSONL, only the derived files.
"""

from __future__ import annotations

import io
import re
import tarfile
from pathlib import Path

from pair.model import Session
from pair.readers import read_any

SESSION_FILE_RX = re.compile(r"[^A-Za-z0-9._-]+")


AGENTS_MD = """# Pair review bundle

This tarball is one PR review, at one head SHA. The files:

- `manifest.json` — per-section status, timings, and any errors.
- `brief.json` — every section as structured data.
- `brief.md` — the human-readable brief rendered from `brief.json`.
- `evidence.json` — extracted facts (asks, plans, decisions, pushback candidates).
- `diff.patch` — the PR diff at the head SHA.
- `risky_hunks.json` — added-line hunks the regex flagged (side effects, exec, network, delete).
- `sessions/*.md` — one readable transcript per matched session, headed by turn refs.
- `sessions/index.json` — metadata for every rendered session.
- `AGENTS.md` — this file.

Refs come in four shapes:

- `pr:body` — a claim backed by the PR description.
- `file:<path>` — a whole file in the diff.
- `hunk:<path>:<n>` — one hunk in the diff.
- `<agent>:<session_id>:<turn_id>` — a specific turn in a matched session
  (`agent` is `claude` or `codex`). Point-in-turn references (message uids)
  use the same shape.

The bundle deliberately does not contain raw JSONL. Every citation has a
matching text block in `sessions/*.md` or in the diff.
"""


def _safe_name(agent: str, session_id: str) -> str:
    stem = f"{agent}-{session_id}"
    return SESSION_FILE_RX.sub("_", stem)[:120]


def render_session_md(sess: Session) -> str:
    """One matched session as reviewable markdown. Message ordering matches the transcript."""
    header = [f"# {sess.agent}:{sess.session_id}"]
    if sess.title:
        header.append(f"title: {sess.title}")
    if sess.cwd:
        header.append(f"cwd: {sess.cwd}")
    if sess.first_command:
        header.append(f"first_command: {sess.first_command}")
    if sess.pr_links:
        header.append("pr_links:")
        header.extend(f"- {u}" for u in sess.pr_links)
    lines = ["\n".join(header), ""]
    for t in sess.turns:
        turn_head = [f"## turn {t.ref}", f"started: {t.started.isoformat()}"]
        if t.branch:
            turn_head.append(f"branch: {t.branch}")
        if t.subagent:
            turn_head.append("subagent: true")
        if t.interrupted:
            turn_head.append("interrupted: true")
        lines.append("\n".join(turn_head))
        lines.append("")
        lines.append(f"**prompt** — {t.prompt.strip()}")
        lines.append("")
        for m in t.messages:
            uid = f" ({t.agent}:{t.session_id}:{m.uid})" if m.uid else ""
            lines.append(f"### {m.role}{uid}")
            lines.append(f"_ts: {m.ts.isoformat()}_")
            lines.append("")
            lines.append(m.text.rstrip())
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _merge(into: Session, other: Session) -> None:
    """Fold ``other``'s turns into ``into``, keeping the transcript in time order.

    A Claude subagent writes its own JSONL file but records the *parent's*
    session id, so its turns carry parent refs and belong in the parent's
    transcript. Sorting by start time interleaves them where they happened.
    """
    into.turns = sorted(into.turns + other.turns, key=lambda t: t.started)
    into.title = into.title or other.title
    into.cwd = into.cwd or other.cwd
    into.first_command = into.first_command or other.first_command
    into.pr_links = list(dict.fromkeys(into.pr_links + other.pr_links))
    # A merged session is only wholly a subagent when every part was.
    into.subagent = into.subagent and other.subagent


def render_sessions(
    session_jsonl_paths: list[str],
    out_dir: Path,
    roles: dict[str, str] | None = None,
) -> list[dict]:
    """Parse each JSONL under ``session_jsonl_paths`` and write one .md per session.

    Files that resolve to the same ``<agent>:<session_id>`` are merged into one
    transcript, because that pair is what a ref names: writing one file per
    input file would make the last file win and silently drop the rest.

    ``roles`` maps ``<agent>:<session_id>`` to the match role the plugin
    recorded in ``brief.json``. Returns the index for ``sessions/index.json``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    roles = roles or {}
    merged: dict[str, Session] = {}
    sources: dict[str, list[str]] = {}
    for path in session_jsonl_paths:
        try:
            sess = read_any(path, messages=True)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if sess is None:
            continue
        sources.setdefault(sess.key, []).append(Path(path).name)
        if sess.key in merged:
            _merge(merged[sess.key], sess)
        else:
            merged[sess.key] = sess

    index: list[dict] = []
    for key, sess in merged.items():
        name = _safe_name(sess.agent, sess.session_id) + ".md"
        (out_dir / name).write_text(render_session_md(sess))
        index.append({
            "agent": sess.agent,
            # `id` and `role` are the contract's names; keep them spelled that way.
            "id": sess.session_id,
            "role": roles.get(key, "unknown"),
            "file": f"{SESSIONS_SUBDIR}/{name}",
            "title": sess.title,
            "cwd": sess.cwd,
            "turns": len(sess.turns),
            "messages": sum(len(t.messages) for t in sess.turns),
            "sources": sorted(sources.get(key, [])),
        })
    return index


def render_brief_md(brief: dict) -> str:
    """The brief as human-readable markdown, one heading per section."""
    lines: list[str] = ["# Review brief"]
    pr = brief.get("pr") or {}
    if pr:
        url = pr.get("url") or ""
        title = pr.get("title") or ""
        lines.append("")
        if url:
            lines.append(f"**PR** [{pr.get('repo')}#{pr.get('number')}]({url}) — {title}")
        else:
            lines.append(f"**PR** {pr.get('repo')}#{pr.get('number')} — {title}")
        if pr.get("head_branch") and pr.get("base_branch"):
            lines.append(f"branch `{pr['head_branch']}` → `{pr['base_branch']}`")
        if brief.get("head_sha"):
            lines.append(f"head_sha `{brief['head_sha']}`")
    order = [
        "summary", "look_first", "side_effects", "plan_vs_result", "pushback",
        "verification", "decisions", "ask", "churn", "loose_ends",
    ]
    sections = brief.get("sections") or {}
    seen = set()
    for name in order + [k for k in sections if k not in order]:
        if name in seen:
            continue
        seen.add(name)
        items = sections.get(name)
        if items is None:
            continue
        lines.append("")
        lines.append(f"## {name}")
        if not items:
            lines.append("")
            lines.append("_no items_")
            continue
        for item in items:
            lines.append("")
            text = (item.get("text") or "").strip() or "(empty)"
            lines.append(f"- {text}")
            refs = item.get("refs") or []
            if refs:
                lines.append(f"  refs: {', '.join(f'`{r}`' for r in refs)}")
            for extra in ("kind", "status", "question", "answer", "path"):
                if item.get(extra) is not None:
                    lines.append(f"  {extra}: {item[extra]}")
    lines.append("")
    return "\n".join(lines)


BUNDLE_INCLUDES = (
    "manifest.json", "brief.json", "brief.md", "evidence.json",
    "diff.patch", "risky_hunks.json", "AGENTS.md",
)
SESSIONS_SUBDIR = "sessions"


def build_bundle(work_dir: Path) -> bytes:
    """Package the derived files as tar.gz. Raw JSONL is deliberately excluded."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in BUNDLE_INCLUDES:
            p = work_dir / name
            if p.exists():
                tar.add(str(p), arcname=name, recursive=False)
        sess_dir = work_dir / SESSIONS_SUBDIR
        if sess_dir.is_dir():
            for entry in sorted(sess_dir.iterdir()):
                if not entry.is_file():
                    continue
                if entry.suffix == ".jsonl":
                    # No raw JSONL in the bundle: only rendered .md and index.json.
                    continue
                tar.add(str(entry), arcname=f"{SESSIONS_SUBDIR}/{entry.name}", recursive=False)
    return buf.getvalue()
