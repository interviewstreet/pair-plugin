"""Deterministic schema-v2 architecture draft and its atomic writer.

`deterministic_v2_draft(skeleton, snapshot, head)` turns the skeleton (hooks/paircli_skeleton.py)
into a v2 DRAFT: the model-writable fields only, with no LLM, no network and no repository read. It
is the input of `finalize_v2(draft, head, snapshot, "deterministic")`, which fills `schema_version`,
`generated_by`, `head_sha`, node `change`/`state`/`stats`, edge `change` and group
`path`/`column`/`order`, then validates. Everything cited is a real hunk range or import line.

  groups  [{id, label}]   the best skeleton groups (12 at most); smaller ones merge into "other"
  nodes   [{id, label, group, kind, description, files, evidence[, pattern]}]
                          one `file` row per top-ranked citable file, one `pattern` row per repeated
                          change (identical change signature) or folded tests/docs/config set that
                          the skeleton found (its members get no file rows; `files` is a sample of
                          up to 12 with real citations), plus one `routine` row per group that has
                          routine files (lockfiles, generated, vendored, binary). A pattern row
                          counts as one row of the group's budget
  edges   [{id, source, target, label, claim, evidence}]
                          file-level imports between rows, new-side and removed (old-side) ones;
                          only unambiguous ones (a Go import is a package, so no file edge
                          unless the package has exactly one changed file). Imports between a row
                          and many members of one pattern collapse into one edge, labelled
                          "imports (N)", with a computed claim and up to 3 sample citations; no edge
                          is drawn to or from a tests/docs/config pattern row
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
import math
import posixpath
from pathlib import Path

from paircli_architecture import MAX_BYTES, require, save_atomic
from paircli_architecture_v2 import (
    ID_PATTERN, MAX_EDGES, MAX_FILES_PER_NODE, MAX_GAPS, MAX_GROUPS, MAX_NODES, MAX_REFS, SnapshotIndex, dicts,
)
from paircli_patterns import MIN_MEMBERS, pattern_label, verify_pattern
from paircli_skeleton import CATEGORY_PENALTY, _clean, _int, _line_kind, _plural, _slug, size_budget

ARCHITECTURE_V2 = "architecture.v2.json"
STATUS_V2 = "architecture-v2-status.json"
TIERS = ("llm", "deterministic", "none")

MAX_NODE_REFS = 3        # citations per file row
MAX_EDGE_SIDE_REFS = 3   # citations per side of an import edge
MAX_PATTERN_SAMPLES = 12  # sample members listed (and cited) on a pattern row
MAX_PATTERN_EDGE_REFS = 3  # sample citations on an edge that stands for several imports
MODIFIED_SOURCE_BONUS = 1.0  # row selection: a modified source file outranks added boilerplate
PATTERN_BONUS = 2.0
CATEGORY_OF_FAMILY = {"tests": "test", "docs": "docs", "config": "config"}
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


def _verified_patterns(raw, cite: _Citations, records: dict) -> list[dict]:
    """The skeleton patterns of a group whose members can be cited, as the validator will recompute them.

    Members that cannot be cited (no hunk range) are dropped; a pattern left with fewer than 3 members,
    or one the validator's own check (`verify_pattern`) would reject, is skipped, so its files stay file rows.
    """
    result = []
    for item in dicts(raw):
        family = item.get("family")
        members = sorted({path for path in item.get("members") or [] if isinstance(path, str) and path in records
                          and cite.usable(path)})
        if len(members) < MIN_MEMBERS:
            continue
        signature = item.get("signature") if family == "same-change" else None
        candidate = {"family": family, "count": len(members), "members": members}
        if signature is not None:
            candidate["signature"] = signature
        if verify_pattern(candidate, cite.index.files):
            continue
        statuses = {records[path]["status"] for path in members}
        status = (next(iter(statuses)) if family == "same-change" and len(statuses) == 1
                  else "added" if statuses == {"added"} else "deleted" if statuses == {"deleted"} else "modified")
        result.append({
            "family": family, "signature": signature, "members": members, "status": status,
            "additions": sum(records[path]["additions"] for path in members),
            "deletions": sum(records[path]["deletions"] for path in members),
        })
    return result


def _pattern_priority(pattern: dict) -> float:
    """Row-selection and ordering score: big repeated changes first; folded tests/docs/config lowest."""
    lines = math.log1p(pattern["additions"] + pattern["deletions"])
    if pattern["family"] == "same-change":
        return round(lines + PATTERN_BONUS + math.log1p(len(pattern["members"])), 4)
    return round(lines - CATEGORY_PENALTY.get(CATEGORY_OF_FAMILY.get(pattern["family"], ""), 0.0), 4)


def _file_priority(record: dict) -> float:
    """Row-selection score of a file: its skeleton score, plus a bonus for edited (not added) source code."""
    bonus = MODIFIED_SOURCE_BONUS if record.get("category") == "source" and record.get("status") in ("modified", "renamed") else 0.0
    return record.get("score", 0) + bonus


def _describe_pattern(pattern: dict) -> str:
    count = len(pattern["members"])
    if pattern["family"] == "same-change":
        status = pattern["status"]
        change = {"deleted": "removes", "added": "adds"}.get(status)
        what = (f"Each {change} the same lines (digits aside)." if change
                else "Each makes the same edit (digits aside).")
        return (f"{count} files with the same change: {status}. {what} "
                f"Together +{pattern['additions']}/-{pattern['deletions']} lines.")
    noun = {"tests": "test", "docs": "doc", "config": "config"}[pattern["family"]]
    return f"{count} {noun} files changed in this area. Together +{pattern['additions']}/-{pattern['deletions']} lines."


def _pattern_node(group_id: str, pattern: dict, records: dict, cite: _Citations, taken: set[str]) -> dict | None:
    """One `pattern` row: the biggest members as a cited sample, every member listed in `pattern.members`."""
    members = pattern["members"]
    ranked = sorted(members, key=lambda path: (-(records[path]["additions"] + records[path]["deletions"]), path))
    samples, refs = [], []
    for path in ranked:
        if len(samples) >= MAX_PATTERN_SAMPLES:
            break
        evidence = cite.evidence(path)
        if evidence:
            samples.append((path, evidence))
    if not samples:
        return None
    # One citation per sample first, then more from the same files while the row has room.
    for depth in range(MAX_NODE_REFS):
        for _path, evidence in samples:
            if len(refs) < MAX_REFS and len(evidence) > depth:
                refs.append(evidence[depth])
    label = pattern_label(pattern["family"], len(members), pattern["status"])
    name = f"same-{pattern['status']}" if pattern["family"] == "same-change" else pattern["family"]
    stem = f"{group_id[:30].rstrip('-_') or 'g'}-{name}"
    node = {
        "id": _identifier(f"{stem}-{_hash(pattern['family'] + chr(0) + members[0], 6)}", taken, "pattern"),
        "label": label, "group": group_id, "kind": "pattern", "description": _describe_pattern(pattern),
        "files": [path for path, _ in samples], "evidence": refs,
        "pattern": {"family": pattern["family"], "count": len(members)},
    }
    if pattern["signature"] is not None:
        node["pattern"]["signature"] = pattern["signature"]
    node["pattern"]["members"] = list(members)
    return node


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
                            "merged_ids": [group["id"] for group in rest],
                            "patterns": [item for group in rest for item in dicts(group.get("patterns"))]}]
    routine_by_group: dict[str, list[str]] = {}
    for record in records.values():
        if record.get("routine") and isinstance(record.get("group"), str):
            routine_by_group.setdefault(record["group"], []).append(record["path"])

    count = len(eligible)
    per_group = min(rows_per_group, max(1, MAX_NODES // count))
    group_ids: set[str] = set()
    node_ids: set[str] = set()
    groups, nodes, row_of, pattern_of = [], [], {}, {}
    unlisted = folded = 0
    pattern_rows = 0
    for group in eligible:
        group_id = _identifier(str(group.get("id")), group_ids, "group")
        routine = sorted(path for source in group.get("merged_ids") or [group["id"]]
                         for path in routine_by_group.get(source, []))
        shown = max(1, per_group - 1) if routine else per_group
        usable = citable(group)
        patterns = _verified_patterns(group.get("patterns"), cite, records)
        members = {path for pattern in patterns for path in pattern["members"]}
        singles = [path for path in usable if path not in members]
        groups.append({"id": group_id, "label": _clean(group.get("label"), 80) or "Changes"})

        # Rows compete for the budget: a pattern row is one row however many files it stands for.
        # `order` is the display rank (the skeleton score order for files); `priority` decides who is cut.
        candidates = [(_file_priority(records[path]), records[path].get("score", 0), path, None) for path in singles]
        candidates += [(_pattern_priority(pattern), _pattern_priority(pattern), pattern["members"][0], pattern)
                       for pattern in patterns]
        chosen = sorted(candidates, key=lambda item: (-item[0], item[2]))[:shown]
        unlisted += len(usable) - sum(1 if item[3] is None else len(item[3]["members"]) for item in chosen)
        for _priority, _order, path, pattern in sorted(chosen, key=lambda item: (-item[1], item[2])):
            if pattern is not None:
                node = _pattern_node(group_id, pattern, records, cite, node_ids)
                if node is None:
                    unlisted += len(pattern["members"])
                    continue
                for member in pattern["members"]:
                    pattern_of[member] = node["id"]
                folded += len(pattern["members"])
                pattern_rows += 1
                nodes.append(node)
                continue
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
    if not any(node["kind"] in ("file", "pattern") for node in nodes):
        return None
    node_ids = {node["id"] for node in nodes}
    row_of = {path: name for path, name in row_of.items() if name in node_ids}
    pattern_of = {path: name for path, name in pattern_of.items() if name in node_ids}
    pattern_nodes = {node["id"]: node for node in nodes if node["kind"] == "pattern"}
    shown_groups = {node["group"] for node in nodes}
    groups = [group for group in groups if group["id"] in shown_groups]

    edges = _edges(skeleton, cite, row_of, pattern_of, pattern_nodes)

    totals = skeleton.get("totals") if isinstance(skeleton.get("totals"), dict) else {}
    routine_total = _int(totals.get("routine_files")) or 0
    no_hunks = sum(1 for path, record in records.items() if not record.get("routine") and not cite.usable(path))
    gaps = []
    if routine_total:
        gaps.append(f"{_plural(routine_total, 'routine file')} (lockfiles, generated, vendored or binary) "
                    "collapsed, not shown individually.")
    if folded:
        gaps.append(f"{_plural(folded, 'changed file')} folded into {_plural(pattern_rows, 'pattern row')} "
                    "(the same change repeated, or tests, docs and config of one area).")
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


def _edges(skeleton: dict, cite: _Citations, row_of: dict[str, str], pattern_of: dict[str, str],
           pattern_nodes: dict[str, dict]) -> list[dict]:
    """Import edges between rows, with the import lines cited on whichever side they sit.

    A file row's imports of other file rows stay one edge per pair. Imports that start or end at members
    of a pattern row collapse into one edge per pair of rows ("imports (N)", a claim counting the members
    it covers, up to 3 sample citations). Nothing is drawn to or from a tests/docs/config pattern row.
    """
    found: dict[tuple[str, str], dict[str, set[int]]] = {}
    grouped: dict[tuple[str, str], dict] = {}
    for edge in dicts(skeleton.get("edges")):
        where = edge.get("cite") if isinstance(edge.get("cite"), dict) else {}
        source, target = edge.get("source_file"), edge.get("target_file")
        side, line = where.get("side"), _int(where.get("line"))
        # A package-level edge has no target file: it never becomes a file-level edge.
        if (not isinstance(source, str) or not isinstance(target, str) or source == target
                or side not in ("new", "old")
                or line is None or where.get("path") != source
                or not any(low <= line <= high for low, high in cite.ranges(source, side))):
            continue
        source_row, target_row = row_of.get(source) or pattern_of.get(source), row_of.get(target) or pattern_of.get(target)
        if source_row is None or target_row is None or source_row == target_row:
            continue
        if source in row_of and target in row_of:
            found.setdefault((source, target), {"new": set(), "old": set()})[side].add(line)
            continue
        if any(pattern_nodes[row]["pattern"]["family"] != "same-change" for row in (source_row, target_row)
               if row in pattern_nodes):
            continue
        item = grouped.setdefault((source_row, target_row), {"sources": set(), "targets": set(), "pairs": set(), "cites": set()})
        item["sources"].add(source)
        item["targets"].add(target)
        item["pairs"].add((source, target))
        item["cites"].add((source, line, side))

    # (weight, sort key, edge) for every candidate; the heaviest MAX_EDGES stay, in a stable order.
    candidates = []
    for (source, target), sides in found.items():
        refs = [{"type": "diff", "path": cite.ref_path(source, side), "start": line, "end": line, "side": side}
                for side in ("new", "old") for line in sorted(sides[side])[:MAX_EDGE_SIDE_REFS]]
        verb = "imports" if sides["new"] else "imported"
        names = [_clean(posixpath.basename(path), 80) or "file" for path in (source, target)]
        candidates.append((sum(len(lines) for lines in sides.values()), (source, target), {
            "id": ("e-" + _hash(source + "\0" + target, 10)),
            "source": row_of[source], "target": row_of[target], "label": "imports",
            "claim": f"{names[0]} {verb} {names[1]}.", "evidence": refs,
        }))
    for (source_row, target_row), item in grouped.items():
        candidates.append((len(item["cites"]), (source_row, target_row),
                           _collapsed_edge(source_row, target_row, item, cite, row_of, pattern_nodes)))
    chosen = sorted(candidates, key=lambda entry: (-entry[0], entry[1]))[:MAX_EDGES]
    taken: set[str] = set()
    edges = []
    for _weight, _key, edge in sorted(chosen, key=lambda entry: entry[1]):
        edge["id"] = _identifier(edge["id"], taken, "e")
        edges.append(edge)
    return edges


def _collapsed_edge(source_row: str, target_row: str, item: dict, cite: _Citations, row_of: dict[str, str],
                    pattern_nodes: dict[str, dict]) -> dict:
    """One edge for all imports between a file row and a pattern row, or between two pattern rows."""
    cites = sorted(item["cites"], key=lambda ref: (ref[2] != "new", ref[0], ref[1]))
    new = [ref for ref in cites if ref[2] == "new"]
    old = [ref for ref in cites if ref[2] == "old"]
    # Up to 3 citations; when both sides exist, keep one removed import so the edge reads as modified.
    picked = (new[:MAX_PATTERN_EDGE_REFS - 1] + old[:1]) if new and old else (new or old)[:MAX_PATTERN_EDGE_REFS]
    refs = [{"type": "diff", "path": cite.ref_path(path, side), "start": line, "end": line, "side": side}
            for path, line, side in sorted(picked, key=lambda ref: (ref[2] != "new", ref[0], ref[1]))]
    imports = "imports" if new else "imported"
    plural = "import" if new else "imported"

    def covers(row: str, members: set) -> str:
        pattern = pattern_nodes[row]["pattern"]
        return f'{len(members)} of the {pattern["count"]} files in "{pattern_nodes[row]["label"]}"'

    if source_row in pattern_nodes and target_row in pattern_nodes:
        claim = f"{covers(source_row, item['sources'])} {plural} {covers(target_row, item['targets'])}."
        count = len(item["pairs"])
    elif target_row in pattern_nodes:
        source = next(iter(item["sources"]))
        claim = f"{_clean(posixpath.basename(source), 80)} {imports} {covers(target_row, item['targets'])}."
        count = len(item["targets"])
    else:
        target = next(iter(item["targets"]))
        claim = f"{covers(source_row, item['sources'])} {plural} {_clean(posixpath.basename(target), 80)}."
        count = len(item["sources"])
    return {"id": "e-" + _hash(source_row + "\0" + target_row, 10), "source": source_row, "target": target_row,
            "label": f"imports ({count})", "claim": _clean(claim, 400), "evidence": refs}


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
