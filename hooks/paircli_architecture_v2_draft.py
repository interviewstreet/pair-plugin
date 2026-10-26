"""Deterministic schema-v2 architecture draft and its atomic writer.

`deterministic_v2_draft(skeleton, snapshot, head)` turns the skeleton (hooks/paircli_skeleton.py)
into a v2 DRAFT: the model-writable fields only, with no LLM, no network and no repository read. It
is the input of `finalize_v2(draft, head, snapshot, "deterministic")`, which fills `schema_version`,
`generated_by`, `head_sha`, node `change`/`state`/`stats`, edge `change` and group
`path`/`column`/`order`, then validates. Everything cited is a real hunk range or import line.

  groups  [{id, label}]   the best skeleton groups (12 at most); smaller ones merge into "other"
  nodes   [{id, label, group, kind, description, files, evidence}]
                          one `file` row per top-ranked citable file, plus one `routine` row per
                          group that has routine files (lockfiles, generated, vendored, binary)
  edges   [{id, source, target, label, claim, evidence}]
                          file-level imports between rows, new-side and removed (old-side) ones
  steps   []              narration is the model's job
  gaps    [str]           what was collapsed, left out or not citable (5 at most)

Unlike the v1 diagram, deleted files get rows (old-side evidence is valid in v2), so a PR that
only deletes files still has a draft. The draft is None for a docs-only PR or when no changed
file has a hunk to cite.

`write_architecture_v2` and friends write `generated_artifacts/architecture.v2.json` (shipped in
the ZIP) and `diagnostics/architecture-v2-status.json` (never shipped), beside the untouched v1 files.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
from pathlib import Path

from paircli_architecture import MAX_BYTES, require, save_atomic
from paircli_architecture_v2 import (
    ID_PATTERN, MAX_EDGES, MAX_FILES_PER_NODE, MAX_GAPS, MAX_GROUPS, MAX_NODES, SnapshotIndex, dicts,
)
from paircli_skeleton import _clean, _int, _line_kind, _plural, _slug, size_budget

ARCHITECTURE_V2 = "architecture.v2.json"
STATUS_V2 = "architecture-v2-status.json"
TIERS = ("llm", "deterministic", "none")

MAX_NODE_REFS = 3        # citations per file row
MAX_EDGE_SIDE_REFS = 3   # citations per side of an import edge
MAX_PATH_CHARS = 300
MAX_GAP_CHARS = 300
OTHER_LABEL = "Other changes"


def _hash(text: str, length: int) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:length]


def _identifier(text: str, taken: set[str], fallback: str) -> str:
    """A pattern-valid id from `text` (made unique among `taken` with a hash suffix) and remember it."""
    base = text if ID_PATTERN.fullmatch(text) else _slug(text)[:64] or fallback
    candidate, length = base, 6
    while candidate in taken or not ID_PATTERN.fullmatch(candidate):
        candidate = f"{base[:64 - length - 1].rstrip('-_') or fallback}-{_hash(text + candidate, length)}"
        length = min(length + 2, 20)
    taken.add(candidate)
    return candidate


def _node_id(group_id: str, path: str, taken: set[str]) -> str:
    """`<group>-<file name>-<hash of the full path>`: readable, stable across runs and unique."""
    name = _slug(posixpath.basename(path))[:28].strip("-_") or "file"
    stem = f"{group_id[:24].rstrip('-_') or 'g'}-{name}"
    return _identifier(f"{stem}-{_hash(path, 6)}", taken, "file")


# --- citations -------------------------------------------------------------------------------

class _Citations:
    """Real citations for changed files, read with the same hunk-range rules the validator applies."""

    def __init__(self, snapshot):
        self.index = SnapshotIndex(snapshot)
        self._usable: dict[str, bool] = {}
        self._ranges: dict[tuple[str, str], list[tuple[int, int]]] = {}

    def file(self, path: str):
        return self.index.files.get(path)

    def ranges(self, path: str, side: str) -> list[tuple[int, int]]:
        key = (path, side)
        if key not in self._ranges:
            file = self.file(path)
            self._ranges[key] = [(low, high) for low, high in self.index.ranges(file, side) if low >= 1] if file else []
        return self._ranges[key]

    def usable(self, path: str) -> bool:
        """The file can be cited: its path fits and some hunk has a new-side or old-side range."""
        if path not in self._usable:
            self._usable[path] = (len(path) <= MAX_PATH_CHARS and self.file(path) is not None
                                  and bool(self.ranges(path, "new") or self.ranges(path, "old")))
        return self._usable[path]

    def ref_path(self, path: str, side: str) -> str:
        """The path to cite: an old-side citation of a renamed file names the old path when it is unambiguous."""
        file = self.file(path)
        old = file.get("old_path") if file else None
        if (side == "old" and isinstance(old, str) and old and old != path and len(old) <= MAX_PATH_CHARS
                and old not in self.index.files):
            return old
        return path

    def evidence(self, path: str) -> list[dict]:
        """Up to MAX_NODE_REFS citations from the file's biggest hunks, narrowed to the changed lines."""
        file = self.file(path)
        if file is None:
            return []
        hunks = []
        for hunk in self.index.hunks(file):
            new_ok = hunk["new_lines"] > 0 and hunk["new_start"] >= 1
            old_ok = hunk["old_lines"] > 0 and hunk["old_start"] >= 1
            if new_ok or old_ok:
                hunks.append((-(hunk["new_lines"] + hunk["old_lines"]), hunk["new_start"], hunk["old_start"], hunk,
                              new_ok, old_ok))
        hunks.sort(key=lambda item: item[:3])
        refs = []
        for _size, _new, _old, hunk, new_ok, old_ok in hunks[:MAX_NODE_REFS]:
            new_no, old_no = hunk["new_start"], hunk["old_start"]
            added, removed = [], []
            for line in dicts(hunk.get("lines")):
                kind = _line_kind(line.get("kind"))
                if kind == "add":
                    added.append(new_no)
                    new_no += 1
                elif kind == "del":
                    removed.append(old_no)
                    old_no += 1
                elif kind == "ctx":
                    new_no += 1
                    old_no += 1
            new_range = (hunk["new_start"], hunk["new_start"] + hunk["new_lines"] - 1)
            old_range = (hunk["old_start"], hunk["old_start"] + hunk["old_lines"] - 1)
            if new_ok and added:
                side, span, lines = "new", new_range, added
            elif old_ok and removed:
                side, span, lines = "old", old_range, removed
            elif new_ok:
                side, span, lines = "new", new_range, []
            else:
                side, span, lines = "old", old_range, []
            first, last = (max(min(lines), span[0]), min(max(lines), span[1])) if lines else span
            if first > last:
                first, last = span
            refs.append({"type": "diff", "path": self.ref_path(path, side), "start": first, "end": last, "side": side})
        unique = {(ref["path"], ref["side"], ref["start"], ref["end"]): ref for ref in refs}
        return sorted(unique.values(), key=lambda ref: (ref["start"], ref["end"], ref["side"], ref["path"]))


# --- the draft -------------------------------------------------------------------------------

def _describe(record: dict) -> str:
    status = record.get("status")
    additions, deletions = record.get("additions", 0), record.get("deletions", 0)
    if status == "renamed" and record.get("old_path"):
        lead = f"Renamed from {_clean(record['old_path'], 200)}"
    else:
        lead = {"added": "Added", "deleted": "Deleted"}.get(status, "Modified")
    return f"{lead}, +{additions}/-{deletions} lines."


def _cap(text: str) -> str:
    return _clean(text, MAX_GAP_CHARS)


def _build(skeleton: dict, snapshot, head) -> dict | None:
    if not isinstance(skeleton, dict) or skeleton.get("docs_only"):
        return None
    pr = snapshot.get("pr") if isinstance(snapshot, dict) and isinstance(snapshot.get("pr"), dict) else {}
    cite = _Citations(snapshot)
    records = {record["path"]: record for record in dicts(skeleton.get("files")) if isinstance(record.get("path"), str)}
    skeleton_groups = [group for group in dicts(skeleton.get("groups")) if isinstance(group.get("files"), list)]
    budget = skeleton.get("budget") if isinstance(skeleton.get("budget"), dict) else size_budget(0, 0)
    rows_per_group = max(1, _int(budget.get("max_rows_per_group")) or 8)

    def citable(group: dict) -> list[str]:
        return [path for path in group["files"] if path in records and cite.usable(path)]

    eligible = [group for group in skeleton_groups if citable(group)]
    if not eligible:
        return None

    # Groups: the best MAX_GROUPS - 1 stay, the rest merge into one "other" group (best files first).
    merged_count = 0
    if len(eligible) > MAX_GROUPS:
        keep, rest = eligible[:MAX_GROUPS - 1], eligible[MAX_GROUPS - 1:]
        merged_count = len(rest)
        paths = sorted((path for group in rest for path in group["files"] if path in records),
                       key=lambda path: (-records[path].get("score", 0), path))
        other_id = "other"
        if other_id in {group["id"] for group in keep}:
            other_id = _identifier("other", {group["id"] for group in keep}, "other")
        eligible = keep + [{"id": other_id, "label": OTHER_LABEL, "files": paths,
                            "merged_ids": [group["id"] for group in rest]}]
    routine_by_group: dict[str, list[str]] = {}
    for record in records.values():
        if record.get("routine") and isinstance(record.get("group"), str):
            routine_by_group.setdefault(record["group"], []).append(record["path"])

    count = len(eligible)
    per_group = min(rows_per_group, max(1, MAX_NODES // count))
    group_ids: set[str] = set()
    node_ids: set[str] = set()
    groups, nodes, row_of = [], [], {}
    unlisted = 0
    for group in eligible:
        group_id = _identifier(str(group.get("id")), group_ids, "group")
        routine = sorted(path for source in group.get("merged_ids") or [group["id"]]
                         for path in routine_by_group.get(source, []))
        shown = max(1, per_group - 1) if routine else per_group
        usable = citable(group)
        unlisted += max(0, len(usable) - shown)
        groups.append({"id": group_id, "label": _clean(group.get("label"), 80) or "Changes"})
        for path in usable[:shown]:
            record = records[path]
            evidence = cite.evidence(path)
            if not evidence:
                unlisted += 1
                continue
            node_id = _node_id(group_id, path, node_ids)
            row_of[path] = node_id
            nodes.append({
                "id": node_id, "label": _clean(posixpath.basename(path), 80) or _clean(path, 80),
                "group": group_id, "kind": "file", "description": _describe(record),
                "files": [path], "evidence": evidence,
            })
        if routine:
            nodes.append({
                "id": _identifier(f"{group_id[:55]}-routine", node_ids, "routine"),
                "label": _plural(len(routine), "routine file"), "group": group_id, "kind": "routine",
                "description": f"{_plural(len(routine), 'routine file')} (lockfiles, generated, vendored or binary) "
                               "changed; not shown individually.",
                "files": [path for path in routine if len(path) <= MAX_PATH_CHARS][:MAX_FILES_PER_NODE],
                "evidence": [],
            })
    nodes = nodes[:MAX_NODES]
    if not any(node["kind"] == "file" for node in nodes):
        return None
    node_ids = {node["id"] for node in nodes}
    row_of = {path: name for path, name in row_of.items() if name in node_ids}
    shown_groups = {node["group"] for node in nodes}
    groups = [group for group in groups if group["id"] in shown_groups]

    edges = _edges(skeleton, cite, records, row_of)

    totals = skeleton.get("totals") if isinstance(skeleton.get("totals"), dict) else {}
    routine_total = _int(totals.get("routine_files")) or 0
    no_hunks = sum(1 for path, record in records.items() if not record.get("routine") and not cite.usable(path))
    gaps = []
    if routine_total:
        gaps.append(f"{_plural(routine_total, 'routine file')} (lockfiles, generated, vendored or binary) "
                    "collapsed, not shown individually.")
    if no_hunks:
        gaps.append(f"{_plural(no_hunks, 'changed file')} with no usable hunk data cannot be cited and "
                    f"{'is' if no_hunks == 1 else 'are'} not shown.")
    if merged_count:
        gaps.append(f"{_plural(merged_count, 'smaller area')} merged into {OTHER_LABEL} to fit the size limit.")
    if unlisted:
        gaps.append(f"{_plural(unlisted, 'changed file')} not listed individually because of the row limit.")
    gaps = [_cap(gap) for gap in gaps[:MAX_GAPS - 1]]
    gaps.append("Generated deterministically from file structure and imports, not from reading the code.")

    files = _int(totals.get("files"))
    files = len(records) if files is None else files
    title = _clean(pr.get("title") if isinstance(pr.get("title"), str) else "", 160) or "Pull request changes"
    return {
        "title": title,
        "description": (f"Changes {_plural(files, 'file')} in {_plural(len(skeleton_groups), 'area')} "
                        f"(+{_int(totals.get('additions')) or 0}/-{_int(totals.get('deletions')) or 0})."),
        "groups": groups, "nodes": nodes, "edges": edges, "steps": [], "gaps": gaps,
    }


def _edges(skeleton: dict, cite: _Citations, records: dict, row_of: dict[str, str]) -> list[dict]:
    """File-level import edges between rows, with the import lines cited on whichever side they sit."""
    found: dict[tuple[str, str], dict[str, set[int]]] = {}
    for edge in dicts(skeleton.get("edges")):
        where = edge.get("cite") if isinstance(edge.get("cite"), dict) else {}
        source, target = edge.get("source_file"), edge.get("target_file")
        side, line = where.get("side"), _int(where.get("line"))
        if (source not in row_of or target not in row_of or source == target or side not in ("new", "old")
                or line is None or where.get("path") != source
                or not any(low <= line <= high for low, high in cite.ranges(source, side))):
            continue
        found.setdefault((source, target), {"new": set(), "old": set()})[side].add(line)
    chosen = sorted(found, key=lambda pair: (-sum(len(lines) for lines in found[pair].values()), pair))[:MAX_EDGES]
    taken: set[str] = set()
    edges = []
    for source, target in sorted(chosen):
        sides = found[(source, target)]
        refs = [{"type": "diff", "path": cite.ref_path(source, side), "start": line, "end": line, "side": side}
                for side in ("new", "old") for line in sorted(sides[side])[:MAX_EDGE_SIDE_REFS]]
        verb = "imports" if sides["new"] else "imported"
        names = [_clean(posixpath.basename(path), 80) or "file" for path in (source, target)]
        edges.append({
            "id": _identifier("e-" + _hash(source + "\0" + target, 10), taken, "e"),
            "source": row_of[source], "target": row_of[target], "label": "imports",
            "claim": f"{names[0]} {verb} {names[1]}.", "evidence": refs,
        })
    return edges


def deterministic_v2_draft(skeleton, snapshot, head) -> dict | None:
    """A v2 draft built from the skeleton and snapshot alone, or None when there is nothing to cite.

    `head` is accepted so call sites mirror `deterministic_architecture`; the draft carries no head
    (finalize_v2 adds it). The output is byte-deterministic. Any unexpected input shape gives None
    rather than an exception, so the caller falls through to "no diagram".
    """
    try:
        return _build(skeleton, snapshot, head)
    except Exception:
        return None


# --- writing ---------------------------------------------------------------------------------

def _log(log, message: str) -> None:
    try:
        log.write(message)
    except Exception:
        pass


def _count(document, key: str) -> int:
    items = document.get(key) if isinstance(document, dict) else None
    return len(items) if isinstance(items, list) else 0


def record_status_v2(artifact_dir: Path, tier: str, reason: str, document, llm_calls, log) -> None:
    """Record which tier produced the v2 diagram, beside the other diagnostics (never in the ZIP)."""
    generated_by = document.get("generated_by") if isinstance(document, dict) else None
    status = {
        "tier": tier, "reason": reason,
        "generated_by": generated_by if isinstance(generated_by, str) else None,
        "counts": {key: _count(document, key) for key in ("groups", "nodes", "edges", "steps")},
        "llm_calls": dict(llm_calls) if isinstance(llm_calls, dict) else {},
    }
    try:
        save_atomic(artifact_dir / "diagnostics", STATUS_V2, json.dumps(status, indent=2, ensure_ascii=False))
    except (OSError, ValueError, TypeError) as error:
        _log(log, f"architecture v2 status not saved ({type(error).__name__})\n")


def remove_stale_architecture_v2(artifact_dir: Path) -> None:
    """An older v2 diagram must not be uploaded as if it described this run."""
    folder = artifact_dir / "generated_artifacts"
    try:
        if not folder.is_symlink():
            (folder / ARCHITECTURE_V2).unlink(missing_ok=True)
    except OSError:
        pass


def write_architecture_v2(artifact_dir: Path, document: dict, log, *, tier: str, reason: str,
                          llm_calls: dict | None = None) -> Path:
    """Atomically write a finalized, validated v2 document and its status file.

    Writes `generated_artifacts/architecture.v2.json`; raises ValueError for a symlinked artifact
    folder, `generated_artifacts` or destination file, an unknown tier or a document over the size
    limit. A failure to write the status file or the log is logged, never raised.
    """
    require(tier in TIERS)
    require(not artifact_dir.is_symlink())
    content = json.dumps(document, ensure_ascii=False)
    require(len(content.encode("utf-8")) <= MAX_BYTES)
    folder = artifact_dir / "generated_artifacts"
    require(not folder.is_symlink() and not (folder / ARCHITECTURE_V2).is_symlink())
    save_atomic(folder, ARCHITECTURE_V2, content)
    record_status_v2(artifact_dir, tier, reason, document, llm_calls, log)
    _log(log, f"wrote generated_artifacts/{ARCHITECTURE_V2} ({tier}; {_count(document, 'nodes')} nodes, "
              f"{_count(document, 'edges')} edges)\n")
    return folder / ARCHITECTURE_V2


def write_no_architecture_v2(artifact_dir: Path, reason: str, log=None, *, llm_calls: dict | None = None) -> None:
    """The "none" tier: remove any older v2 diagram and record why there is no new one."""
    remove_stale_architecture_v2(artifact_dir)
    if log is None:
        log = _NullLog()
    record_status_v2(artifact_dir, "none", reason, None, llm_calls, log)
    _log(log, f"architecture v2 skipped: {reason}\n")


class _NullLog:
    def write(self, message: str) -> None:
        pass
