"""Architecture artifact schema v2: normalize, enrich and validate against the PR snapshot.

The model narrates and code computes. `enrich_v2` overwrites every computed field from the
snapshot, `validate_v2` recomputes them and rejects any disagreement, so a hand-edited or
unenriched document cannot pass. See docs/architecture-v2-contract.md.
"""
from __future__ import annotations

import copy
import json
import re

from paircli_architecture import IDENTIFIER, KINDS, MAX_BYTES, ArchitectureError
from paircli_patterns import api_surface, verify_pattern
from paircli_snapshot import snapshot_binds_head

SCHEMA_VERSION = 2
GENERATED_BY = ("llm", "deterministic")
NODE_KINDS = KINDS | {"file", "routine", "pattern"}
WHENS = {"before", "after"}
SIDES = {"new", "old"}

MAX_GROUPS, MAX_NODES, MAX_EDGES, MAX_STEPS, MAX_GAPS = 12, 60, 60, 24, 5
MAX_FILES_PER_NODE, MAX_REFS = 15, 12
MAX_PATTERN_SAMPLES = 12
MAX_STEP_NODES = 16
MAX_COLUMN = 8
MAX_WARNINGS = 100

# Key order is also the canonical output order, so enriched documents do not depend on the
# order a model happened to write keys in.
TOP_KEYS = ("schema_version", "generated_by", "head_sha", "title", "description",
            "groups", "nodes", "edges", "steps", "gaps")
# Optional curated map on top of the detailed graph; its own module normalizes and validates it
# (docs/architecture-v2-overview.md).
OPTIONAL_TOP_KEYS = ("overview",)
ALL_TOP_KEYS = TOP_KEYS + OPTIONAL_TOP_KEYS
GROUP_KEYS = ("id", "label", "path", "column", "order")
NODE_KEYS = ("id", "label", "group", "kind", "description", "files", "evidence", "pattern",
             "change", "state", "stats")
# `pattern` is required on a pattern row and not allowed on any other.
OPTIONAL_NODE_KEYS = frozenset({"pattern"})
PATTERN_KEYS = ("family", "count", "signature", "members")
EDGE_KEYS = ("id", "source", "target", "label", "claim", "change", "evidence")
STEP_KEYS = ("id", "when", "edge", "nodes", "text", "flags", "evidence")
EVIDENCE_KEYS = ("type", "path", "start", "end", "side")
STATS_KEYS = ("additions", "deletions", "files", "api_added", "api_removed")
# `edge` is the only optional key; everything else must be present.
STEP_REQUIRED = frozenset(STEP_KEYS) - {"edge"}

ID_PATTERN = re.compile(IDENTIFIER)
HEAD_PATTERN = re.compile(r"[a-f0-9]{40}")


# --- small helpers -----------------------------------------------------------------------

def join(path: str, key) -> str:
    return f"{path}.{key}" if path else str(key)


def is_text(value, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum


def is_int(value) -> bool:
    return type(value) is int


def one_of(value, allowed) -> bool:
    return isinstance(value, str) and value in allowed


def strictly_equal(actual, expected) -> bool:
    """Equality that does not let True pass for 1 or 1.0 for 1."""
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and set(actual) == set(expected)
                and all(strictly_equal(actual[key], value) for key, value in expected.items()))
    return type(actual) is type(expected) and actual == expected


def ordered(item: dict, keys: tuple) -> dict:
    """Known keys first in canonical order, then anything else (which validation rejects)."""
    result = {key: item[key] for key in keys if key in item}
    result.update({key: value for key, value in item.items() if key not in result})
    return result


def dicts(items) -> list[dict]:
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def silent(condition, path, reason) -> bool:
    """A `check` that records nothing, for computing from possibly malformed documents."""
    return bool(condition)


# --- normalize ---------------------------------------------------------------------------

def normalize_v2(document):
    """Strip keys outside the contract and fill the two permitted defaults; never raises."""
    if not isinstance(document, dict):
        return document, ["$: expected a JSON object"]
    warnings: list[str] = []

    def strip(item, allowed, path):
        if not isinstance(item, dict):
            return item
        for key in item:
            if key not in allowed:
                if len(warnings) < MAX_WARNINGS:
                    warnings.append(f"{join(path, str(key)[:64])}: removed unexpected key")
                elif len(warnings) == MAX_WARNINGS:
                    warnings.append("further removed keys not listed")
        return {key: value for key, value in item.items() if key in allowed}

    def strip_evidence(item, path):
        refs = item.get("evidence")
        if not isinstance(refs, list):
            return
        cleaned = []
        for index, ref in enumerate(refs):
            ref = strip(ref, EVIDENCE_KEYS, f"{path}.evidence[{index}]")
            if isinstance(ref, dict):
                ref.setdefault("side", "new")
            cleaned.append(ref)
        item["evidence"] = cleaned

    def strip_all(items, allowed, name, with_evidence=True, defaults=None):
        if not isinstance(items, list):
            return items
        cleaned = []
        for index, item in enumerate(items):
            path = f"{name}[{index}]"
            item = strip(item, allowed, path)
            if isinstance(item, dict):
                for key, value in (defaults or {}).items():
                    item.setdefault(key, copy.deepcopy(value))
                if with_evidence:
                    strip_evidence(item, path)
                if isinstance(item.get("stats"), dict):
                    item["stats"] = strip(item["stats"], STATS_KEYS, f"{path}.stats")
            if isinstance(item, dict) and name == "nodes" and "pattern" in item:
                if item.get("kind") == "pattern":
                    item["pattern"] = strip(item["pattern"], PATTERN_KEYS, f"{path}.pattern")
                else:
                    del item["pattern"]
                    if len(warnings) < MAX_WARNINGS:
                        warnings.append(f"{path}.pattern: removed from a row that is not a pattern row")
            cleaned.append(item)
        return cleaned

    cleaned = strip(copy.deepcopy(document), ALL_TOP_KEYS, "")
    if "overview" in cleaned:
        # The overview module owns its own shape; lazy import because it imports this module.
        try:
            from paircli_overview import normalize_overview
            cleaned["overview"], overview_warnings = normalize_overview(cleaned["overview"])
            room = max(0, MAX_WARNINGS - len(warnings))
            warnings.extend(overview_warnings[:room])
        except Exception:
            warnings.append("overview: could not be normalized")
    for key in ("groups", "nodes", "edges", "steps"):
        if key in cleaned:
            allowed = {"groups": GROUP_KEYS, "nodes": NODE_KEYS, "edges": EDGE_KEYS, "steps": STEP_KEYS}[key]
            cleaned[key] = strip_all(cleaned[key], allowed, key, with_evidence=key != "groups",
                                     defaults={"flags": []} if key == "steps" else None)
    return cleaned, warnings


# --- snapshot index ----------------------------------------------------------------------

class SnapshotIndex:
    """Read-only lookup of changed files, hunk ranges and per-line kinds from snapshot.pr."""

    def __init__(self, snapshot):
        pr = snapshot.get("pr") if isinstance(snapshot, dict) else None
        self.pr = pr if isinstance(pr, dict) else {}
        self.files: dict[str, dict] = {}
        self.old_paths: dict[str, dict] = {}
        for file in dicts(self.pr.get("files")):
            if isinstance(file.get("path"), str) and file["path"]:
                self.files.setdefault(file["path"], file)
                if isinstance(file.get("old_path"), str) and file["old_path"]:
                    self.old_paths.setdefault(file["old_path"], file)
        self._lines: dict[str, dict] = {}
        self._api: dict[str, dict] = {}

    def api(self, file: dict) -> dict:
        """`api_surface` of one changed file, computed once."""
        cached = self._api.get(file["path"])
        if cached is None:
            cached = self._api[file["path"]] = api_surface(file)
        return cached

    def hunks(self, file: dict) -> list[dict]:
        keys = ("old_start", "old_lines", "new_start", "new_lines")
        return [hunk for hunk in dicts(file.get("hunks")) if all(is_int(hunk.get(key)) for key in keys)]

    def ranges(self, file: dict, side: str) -> list[tuple[int, int]]:
        start, count = (f"{side}_start", f"{side}_lines")
        return [(hunk[start], hunk[start] + hunk[count] - 1) for hunk in self.hunks(file) if hunk[count] > 0]

    def lines(self, file: dict) -> dict:
        """`{"new": {line: kind}, "old": {line: kind}, "additions": n, "deletions": n}` for one file.

        `to_old` and `to_new` map the line numbers of context lines between the two sides.
        """
        cached = self._lines.get(file["path"])
        if cached is not None:
            return cached
        result = {"new": {}, "old": {}, "additions": 0, "deletions": 0, "to_old": {}, "to_new": {}}
        for hunk in self.hunks(file):
            new, old = hunk["new_start"], hunk["old_start"]
            for line in dicts(hunk.get("lines")):
                kind = line.get("kind")
                if kind == "ctx":
                    result["new"][new] = result["old"][old] = "ctx"
                    result["to_old"][new], result["to_new"][old] = old, new
                    new, old = new + 1, old + 1
                elif kind == "add":
                    result["new"][new] = "add"
                    result["additions"] += 1
                    new += 1
                elif kind == "del":
                    result["old"][old] = "del"
                    result["deletions"] += 1
                    old += 1
        self._lines[file["path"]] = result
        return result


# --- computed fields ---------------------------------------------------------------------

def pattern_members(node: dict) -> list | None:
    """The member list of a pattern row (any entries, unchecked), or None for any other row."""
    pattern = node.get("pattern") if node.get("kind") == "pattern" else None
    members = pattern.get("members") if isinstance(pattern, dict) else None
    return members if isinstance(members, list) else None


def node_facts(node: dict, index: SnapshotIndex) -> dict:
    """`change`, `state` and `stats` of one node from the snapshot status and hunk lines of its files.

    A pattern row covers all its `pattern.members`; `files` is only a sample of them.
    """
    members = pattern_members(node)
    names = members if members is not None else node.get("files") if isinstance(node.get("files"), list) else []
    resolved = list({name: index.files[name] for name in names if isinstance(name, str) and name in index.files}.values())
    statuses = [file.get("status") for file in resolved]
    if not statuses:
        change = "unchanged"
    elif all(status == "added" for status in statuses):
        change = "added"
    elif all(status == "deleted" for status in statuses):
        change = "removed"
    else:
        change = "modified"
    state = "routine" if node.get("kind") == "routine" else "changed" if change != "unchanged" else "context"
    counts = [index.lines(file) for file in resolved]
    api = [index.api(file) for file in resolved]
    return {"change": change, "state": state,
            "stats": {"files": len(resolved),
                      "additions": sum(count["additions"] for count in counts),
                      "deletions": sum(count["deletions"] for count in counts),
                      "api_added": sum(item["added"] for item in api),
                      "api_removed": sum(item["removed"] for item in api)}}


def edge_change(edge: dict, index: SnapshotIndex) -> str:
    """`change` of an edge from the kinds of the diff lines its valid citations point at.

    Every cited line is context (neither added nor deleted): `unchanged`. Added lines and no deleted
    one: `added` when the citations are only added new-side lines, else `modified`. Deleted lines and
    no added one: `removed` when the citations are only old-side, else `modified`. Both: `modified`.
    """
    added = deleted = False
    new_side = old_side = False
    all_added = True
    refs = edge.get("evidence")
    for ref in refs[:MAX_REFS] if isinstance(refs, list) else []:
        valid = check_ref(ref, "", index, silent)
        if valid is None:
            continue
        file, side, start, end = valid
        kinds = index.lines(file)[side]
        cited = [kinds.get(line) for line in range(start, end + 1)]
        if side == "old":
            old_side = True
            deleted = deleted or "del" in cited
            continue
        new_side = True
        added = added or "add" in cited
        all_added = all_added and all(kind == "add" for kind in cited)
    if not (added or deleted):
        return "unchanged"
    if added and not deleted:
        return "added" if all_added and not old_side else "modified"
    if deleted and not added:
        return "removed" if not new_side else "modified"
    return "modified"


def common_directory(files: list[str]) -> str:
    """Longest directory prefix shared by every file; empty when there is none."""
    prefix = None
    for name in files:
        parts = name.split("/")[:-1]
        if prefix is None:
            prefix = parts
        else:
            shared = 0
            while shared < min(len(prefix), len(parts)) and prefix[shared] == parts[shared]:
                shared += 1
            prefix = prefix[:shared]
    return "/".join(prefix or [])


def compute_layout(groups, nodes, edges) -> dict[str, dict]:
    """Group id -> `{path, column, order}` using a layered layout over the group graph.

    Cycles are broken by a depth-first search that visits groups and neighbours sorted by
    (path, id) and discards the edge that closes a loop, so the result never depends on the
    order of the input lists. The search is iterative so a hostile draft cannot overflow the stack.
    """
    group_ids = []
    for group in dicts(groups):
        if isinstance(group.get("id"), str) and group["id"] not in group_ids:
            group_ids.append(group["id"])
    members: dict[str, list[str]] = {name: [] for name in group_ids}
    node_group: dict[str, str] = {}
    for node in dicts(nodes):
        name, group = node.get("id"), node.get("group")
        if isinstance(name, str) and name not in node_group and isinstance(group, str) and group in members:
            node_group[name] = group
            files = pattern_members(node)
            files = files if files is not None else node.get("files") if isinstance(node.get("files"), list) else []
            members[group] += [file for file in files if isinstance(file, str)]
    paths = {name: common_directory(files) for name, files in members.items()}
    rank = {name: (paths[name], name) for name in group_ids}

    successors: dict[str, set[str]] = {name: set() for name in group_ids}
    for edge in dicts(edges):
        source, target = edge.get("source"), edge.get("target")
        source = node_group.get(source) if isinstance(source, str) else None
        target = node_group.get(target) if isinstance(target, str) else None
        if source is not None and target is not None and source != target:
            successors[source].add(target)

    # Depth-first search: a neighbour still on the stack closes a cycle, so that edge is dropped.
    kept: dict[str, list[str]] = {name: [] for name in group_ids}
    state: dict[str, int] = {}
    finished: list[str] = []
    for root in sorted(group_ids, key=rank.get):
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(sorted(successors[root], key=rank.get)))]
        while stack:
            name, neighbours = stack[-1]
            for neighbour in neighbours:
                if state.get(neighbour) == 1:
                    continue
                kept[name].append(neighbour)
                if neighbour not in state:
                    state[neighbour] = 1
                    stack.append((neighbour, iter(sorted(successors[neighbour], key=rank.get))))
                    break
            else:
                state[name] = 2
                finished.append(name)
                stack.pop()
    columns = {name: 0 for name in group_ids}
    for name in reversed(finished):
        for neighbour in kept[name]:
            columns[neighbour] = max(columns[neighbour], columns[name] + 1)

    layout = {}
    by_column: dict[int, list[str]] = {}
    for name in group_ids:
        by_column.setdefault(min(columns[name], MAX_COLUMN), []).append(name)
    for column, names in by_column.items():
        for position, name in enumerate(sorted(names, key=rank.get)):
            layout[name] = {"path": paths[name], "column": column, "order": position}
    return layout


# --- enrich ------------------------------------------------------------------------------

def step_side(when) -> str | None:
    """The side a step's citations must be on: a `before` step shows the old code, an `after` step the new."""
    return {"before": "old", "after": "new"}.get(when) if isinstance(when, str) else None


def convert_ref(ref, wanted: str, index: SnapshotIndex) -> list[dict] | None:
    """Re-express one citation on the `wanted` side. None: leave it (it is not a valid citation).

    A citation already on that side is kept. One on the other side converts only when every line it
    cites is a context line, which exists on both sides under another number; a context run that is
    not contiguous on the wanted side becomes several citations. A citation that touches an added line
    (cited as `old`: a deleted line) cannot support the other view and gives `[]`.
    """
    valid = check_ref(ref, "", index, silent)
    if valid is None:
        return None
    file, side, start, end = valid
    if side == wanted:
        return [ref]
    facts = index.lines(file)
    kinds, mapping = facts[side], facts["to_old" if side == "new" else "to_new"]
    lines = range(start, end + 1)
    if any(kinds.get(line) != "ctx" or line not in mapping for line in lines):
        return []
    mapped = [mapping[line] for line in lines]
    runs = [[mapped[0], mapped[0]]]
    for line in mapped[1:]:
        if line == runs[-1][1] + 1:
            runs[-1][1] = line
        else:
            runs.append([line, line])
    return [{**ref, "start": first, "end": last, "side": wanted} for first, last in runs]


def convert_step_evidence(step: dict, index: SnapshotIndex, position: int, notes: list | None) -> None:
    """Move a step's citations to the side its `when` needs, dropping those that cannot move."""
    wanted, refs = step_side(step.get("when")), step.get("evidence")
    if wanted is None or not isinstance(refs, list):
        return
    result = []
    for number, ref in enumerate(refs):
        converted = convert_ref(ref, wanted, index)
        if converted is None:
            result.append(ref)  # not a valid citation: validation reports it
            continue
        if not converted and notes is not None and len(notes) < MAX_WARNINGS:
            other = "new" if wanted == "old" else "old"
            notes.append(f"steps[{position}].evidence[{number}]: a {step['when']} step cannot cite {other}-side "
                         f"lines that were {'added' if other == 'new' else 'deleted'}; citation dropped")
        for item in converted or []:
            if item not in result:
                result.append(item)
    step["evidence"] = result[:MAX_REFS]


def enrich_v2(draft, head: str, snapshot, generated_by: str = "llm", notes: list | None = None):
    """Return a copy of `draft` with every code-owned field recomputed; leaves malformed parts alone.

    Step citations move to the side the step's `when` needs (see `convert_ref`); each citation that
    has to be dropped is described in `notes` when a list is given.
    """
    if not one_of(generated_by, GENERATED_BY):
        raise ArchitectureError([{"path": "generated_by", "reason": f"must be one of {list(GENERATED_BY)}"}])
    if not isinstance(draft, dict):
        return draft
    document = copy.deepcopy(draft)
    index = SnapshotIndex(snapshot)
    document.update({"schema_version": SCHEMA_VERSION, "generated_by": generated_by, "head_sha": head})

    nodes = document.get("nodes")
    for position, node in enumerate(nodes if isinstance(nodes, list) else []):
        if isinstance(node, dict):
            node.update(node_facts(node, index))
            nodes[position] = ordered(node, NODE_KEYS)
    edges = document.get("edges")
    for position, edge in enumerate(edges if isinstance(edges, list) else []):
        if isinstance(edge, dict):
            edge["change"] = edge_change(edge, index)
            edges[position] = ordered(edge, EDGE_KEYS)
    steps = document.get("steps")
    for position, step in enumerate(steps if isinstance(steps, list) else []):
        if isinstance(step, dict):
            step["flags"] = []
            convert_step_evidence(step, index, position, notes)
            steps[position] = ordered(step, STEP_KEYS)
    for collection in (nodes, edges, steps):
        for item in collection if isinstance(collection, list) else []:
            refs = item.get("evidence") if isinstance(item, dict) else None
            for position, ref in enumerate(refs if isinstance(refs, list) else []):
                if isinstance(ref, dict):
                    refs[position] = ordered(ref, EVIDENCE_KEYS)

    groups = document.get("groups")
    if isinstance(groups, list):
        used = {node["group"] for node in dicts(nodes) if isinstance(node.get("group"), str)}
        # A well-formed group nobody refers to is dropped; malformed ones stay for validation to report.
        groups[:] = [group for group in groups
                     if not (isinstance(group, dict) and isinstance(group.get("id"), str) and group["id"] not in used)]
        layout = compute_layout(groups, nodes, edges)
        for position, group in enumerate(groups):
            if isinstance(group, dict):
                if isinstance(group.get("id"), str):
                    group.update(layout.get(group["id"], {}))
                groups[position] = ordered(group, GROUP_KEYS)
    return ordered(document, ALL_TOP_KEYS)


# --- validate ----------------------------------------------------------------------------

def check_ref(ref, where: str, index: SnapshotIndex, check):
    """Validate one citation; return `(file, side, start, end)` when it is fully valid, else None."""
    if not check(isinstance(ref, dict), where, "must be an object"):
        return None
    ok = True
    for key in EVIDENCE_KEYS:
        ok &= check(key in ref, join(where, key), "missing")
    for key in sorted(set(ref) - set(EVIDENCE_KEYS), key=str):
        ok &= check(False, join(where, str(key)[:64]), "unexpected key")
    ok &= check(ref.get("type") == "diff", join(where, "type"), 'must be "diff"')
    ok &= check(is_text(ref.get("path"), 300), join(where, "path"), "must be 1-300 characters")
    ok &= check(one_of(ref.get("side"), SIDES), join(where, "side"), 'must be "new" or "old"')
    start, end = ref.get("start"), ref.get("end")
    ok &= check(is_int(start) and is_int(end) and 1 <= start <= end, join(where, "start"),
                "start and end must be integers with 1 <= start <= end")
    if not ok:
        return None
    path, side = ref["path"], ref["side"]
    file = index.files.get(path)
    if file is None and side == "old":
        file = index.old_paths.get(path)
    if not check(file is not None, join(where, "path"),
                 "must be a file changed in this PR" if side == "new"
                 else "must be a changed file or the old path of a renamed one"):
        return None
    if not check(any(low <= start <= end <= high for low, high in index.ranges(file, side)),
                 join(where, "start"), f"lines must lie inside one {side}-side hunk of the cited file"):
        return None
    return file, side, start, end


def check_evidence(refs, path: str, index: SnapshotIndex, check) -> list[tuple]:
    """Validate a citation list; return the valid citations."""
    if not check(isinstance(refs, list) and len(refs) <= MAX_REFS, path,
                 f"must be a list of at most {MAX_REFS} diff citations"):
        return []
    valid = [check_ref(ref, f"{path}[{position}]", index, check) for position, ref in enumerate(refs)]
    return [ref for ref in valid if ref is not None]


def check_pattern(node: dict, path: str, index: SnapshotIndex, check) -> None:
    """Validate a pattern row's `pattern` object, recomputing every claim from the snapshot."""
    for error in verify_pattern(node.get("pattern"), index.files):
        check(False, join(path, error["path"]), error["reason"])
    pattern = node.get("pattern")
    if not isinstance(pattern, dict):
        return
    for key in sorted(set(pattern) - set(PATTERN_KEYS), key=str):
        check(False, join(path, f"pattern.{str(key)[:64]}"), "unexpected key")
    # `remainder` rows are verified by verify_pattern (3 to 500 existing, unique members, no signature
    # key); check_pattern_ownership keeps them disjoint from every other row.
    if pattern.get("family") != "same-change":
        check(pattern.get("signature") is None, join(path, "pattern.signature"),
              "only a same-change pattern has a signature")
    members = pattern.get("members")
    members = set(item for item in members if isinstance(item, str)) if isinstance(members, list) else set()
    files = node.get("files")
    for offset, name in enumerate(files if isinstance(files, list) else []):
        if isinstance(name, str):
            check(name in members, f"{path}.files[{offset}]", "a pattern row's files must be members of its pattern")


def check_pattern_ownership(nodes: list, check) -> None:
    """A file belongs to one row: pattern members are unique across pattern rows and not listed elsewhere."""
    owner: dict[str, int] = {}
    for position, node in enumerate(nodes):
        members = pattern_members(node) if isinstance(node, dict) else None
        for offset, name in enumerate(members or []):
            if not isinstance(name, str):
                continue
            first = owner.setdefault(name, position)
            if first != position:
                check(False, f"nodes[{position}].pattern.members[{offset}]",
                      f"{name} is already a member of nodes[{first}]")
    for position, node in enumerate(nodes):
        if not isinstance(node, dict) or node.get("kind") == "pattern" or not isinstance(node.get("files"), list):
            continue
        for offset, name in enumerate(node["files"]):
            if isinstance(name, str) and name in owner:
                check(False, f"nodes[{position}].files[{offset}]",
                      f"{name} is a member of the pattern row nodes[{owner[name]}]")


class Graph:
    """What step validation needs to know about an otherwise checked graph."""

    def __init__(self, index: SnapshotIndex):
        self.index = index
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.node_change: dict[str, str] = {}
        self.edge_change: dict[str, str] = {}


def check_graph(document: dict, index: SnapshotIndex, check) -> Graph:
    """Check groups, nodes and edges and everything computed from them; returns the lookup for steps."""
    graph = Graph(index)

    def items(key: str, limit: int) -> list:
        if key not in document:  # already reported as missing
            return []
        value = document[key]
        return value if check(isinstance(value, list) and len(value) <= limit, key,
                              f"must be a list of at most {limit}") else []

    def keys_ok(item: dict, expected: tuple, path: str) -> None:
        for key in expected:
            check(key in item, join(path, key), "missing")
        for key in sorted(set(item) - set(expected), key=str):
            check(False, join(path, str(key)[:64]), "unexpected key")

    def identifier(item: dict, path: str, seen: set) -> str | None:
        value = item.get("id")
        if not check(isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None, join(path, "id"),
                     "must start with a letter and use letters, digits, _ or - (max 64)"):
            return None
        check(value not in seen, join(path, "id"), "duplicate id")
        seen.add(value)
        return value

    def computed(item: dict, key: str, expected, path: str) -> None:
        if key in item:
            check(strictly_equal(item[key], expected), join(path, key),
                  f"must be {json.dumps(expected)} as computed from the diff")

    groups, nodes, edges = items("groups", MAX_GROUPS), items("nodes", MAX_NODES), items("edges", MAX_EDGES)
    group_ids: set[str] = set()
    for position, group in enumerate(groups):
        path = f"groups[{position}]"
        if not check(isinstance(group, dict), path, "must be an object"):
            continue
        keys_ok(group, GROUP_KEYS, path)
        identifier(group, path, group_ids)
        check(is_text(group.get("label"), 80), join(path, "label"), "must be 1-80 characters")
        check(isinstance(group.get("path"), str), join(path, "path"), "must be a string")
        check(is_int(group.get("column")), join(path, "column"), "must be an integer")
        check(is_int(group.get("order")), join(path, "order"), "must be an integer")

    check(bool(nodes), "nodes", "must contain at least one node")
    node_ids: set[str] = set()
    refs_by_node: dict[int, list] = {}
    facts_by_node: dict[int, dict] = {}
    for position, node in enumerate(nodes):
        path = f"nodes[{position}]"
        if not check(isinstance(node, dict), path, "must be an object"):
            continue
        keys_ok(node, tuple(key for key in NODE_KEYS if key not in OPTIONAL_NODE_KEYS or node.get("kind") == "pattern"),
                path)
        name = identifier(node, path, node_ids)
        if name is not None and name not in graph.nodes:
            graph.nodes[name] = node
        check(is_text(node.get("label"), 80), join(path, "label"), "must be 1-80 characters")
        check(isinstance(node.get("group"), str) and node["group"] in group_ids, join(path, "group"),
              "must be the id of a declared group")
        check(one_of(node.get("kind"), NODE_KINDS), join(path, "kind"), f"must be one of {sorted(NODE_KINDS)}")
        check(is_text(node.get("description"), 1200), join(path, "description"), "must be 1-1200 characters")
        files = node.get("files")
        is_pattern = node.get("kind") == "pattern"
        limit = MAX_PATTERN_SAMPLES if is_pattern else MAX_FILES_PER_NODE
        if check(isinstance(files, list) and len(files) <= limit, join(path, "files"),
                 f"must be a list of at most {limit}" + (" sample members" if is_pattern else "")):
            for offset, name_of_file in enumerate(files):
                where = f"{path}.files[{offset}]"
                if check(is_text(name_of_file, 300), where, "must be 1-300 characters"):
                    check(files.index(name_of_file) == offset, where, "duplicate file")
        if is_pattern and "pattern" in node:
            check_pattern(node, path, index, check)
        refs_by_node[position] = check_evidence(node.get("evidence"), join(path, "evidence"), index, check)
        facts = facts_by_node[position] = node_facts(node, index)
        if name is not None and name not in graph.node_change:
            graph.node_change[name] = facts["change"]
        for key, value in facts.items():
            computed(node, key, value, path)

    edge_ids: set[str] = set()
    cited_endpoints: set[str] = set()
    for position, edge in enumerate(edges):
        path = f"edges[{position}]"
        if not check(isinstance(edge, dict), path, "must be an object"):
            continue
        keys_ok(edge, EDGE_KEYS, path)
        name = identifier(edge, path, edge_ids)
        if name is not None and name not in graph.edges:
            graph.edges[name] = edge
        check(is_text(edge.get("label"), 80), join(path, "label"), "must be 1-80 characters")
        check(is_text(edge.get("claim"), 400), join(path, "claim"), "must be 1-400 characters")
        source, target = edge.get("source"), edge.get("target")
        for end, value in (("source", source), ("target", target)):
            check(isinstance(value, str) and value in node_ids, join(path, end), "must be an existing node id")
        check(not (isinstance(source, str) and source == target), path, "source and target must differ")
        refs = check_evidence(edge.get("evidence"), join(path, "evidence"), index, check)
        if check(bool(refs), join(path, "evidence"), "needs at least one valid diff citation"):
            cited_endpoints.update(value for value in (source, target) if isinstance(value, str))
        change = edge_change(edge, index)
        if name is not None and name not in graph.edge_change:
            graph.edge_change[name] = change
        computed(edge, "change", change, path)

    check_pattern_ownership(nodes, check)

    for position, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        path = f"nodes[{position}]"
        refs = refs_by_node[position]
        if node.get("kind") == "routine":
            continue
        # Unchanged and external nodes only appear because an edge points at them.
        exempt = (facts_by_node[position]["change"] == "unchanged" or node.get("kind") == "external") \
            and isinstance(node.get("id"), str) and node["id"] in cited_endpoints
        check(bool(refs) or exempt, join(path, "evidence"), "needs at least one valid diff citation")
        cited_files = {file["path"] for file, _side, _start, _end in refs}
        files = node.get("files")
        limit = MAX_PATTERN_SAMPLES if node.get("kind") == "pattern" else MAX_FILES_PER_NODE
        for offset, name_of_file in enumerate(files if isinstance(files, list) and len(files) <= limit else []):
            if not isinstance(name_of_file, str):
                continue
            where = f"{path}.files[{offset}]"
            if check(name_of_file in index.files, where, "must be a file changed in this PR"):
                check(name_of_file in cited_files, where, "needs a valid diff citation of this file")

    layout = compute_layout(groups, nodes, edges)
    declared = {node["group"] for node in dicts(nodes) if isinstance(node.get("group"), str)}
    for position, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("id"), str):
            continue
        path = f"groups[{position}]"
        check(group["id"] in declared, path, "group has no nodes")
        for key, value in layout.get(group["id"], {}).items():
            computed(group, key, value, path)
    return graph


def check_step(step, path: str, graph: Graph, check) -> None:
    """Validate one step against the graph and snapshot; ids are unique-checked by the caller."""
    if not check(isinstance(step, dict), path, "must be an object"):
        return
    for key in sorted(STEP_REQUIRED - set(step)):
        check(False, join(path, key), "missing")
    for key in sorted(set(step) - set(STEP_KEYS), key=str):
        check(False, join(path, str(key)[:64]), "unexpected key")
    check(isinstance(step.get("id"), str) and ID_PATTERN.fullmatch(step["id"]) is not None, join(path, "id"),
          "must start with a letter and use letters, digits, _ or - (max 64)")
    when = step.get("when")
    check(one_of(when, WHENS), join(path, "when"), 'must be "before" or "after"')
    check(is_text(step.get("text"), 400), join(path, "text"), "must be 1-400 characters")
    check(isinstance(step.get("flags"), list) and step["flags"] == [], join(path, "flags"),
          "must be an empty list: flags are not generated yet")
    refs = check_evidence(step.get("evidence"), join(path, "evidence"), graph.index, check)
    check(bool(refs), join(path, "evidence"), "needs at least one valid diff citation")
    wanted = step_side(when)
    if wanted is not None:
        for offset, ref in enumerate(step.get("evidence") if isinstance(step.get("evidence"), list) else []):
            valid = check_ref(ref, "", graph.index, silent)
            if valid is not None:
                check(valid[1] == wanted, f"{path}.evidence[{offset}].side",
                      f'a {when} step must cite {wanted}-side lines (side "{wanted}")')

    # Elements of a view that does not show them: removed things are absent after, added ones before.
    hidden = "removed" if when == "after" else "added" if when == "before" else None
    members = step.get("nodes")
    if check(isinstance(members, list) and 1 <= len(members) <= MAX_STEP_NODES, join(path, "nodes"),
             f"must list 1-{MAX_STEP_NODES} node ids"):
        for offset, name in enumerate(members):
            where = f"{path}.nodes[{offset}]"
            if check(isinstance(name, str) and name in graph.nodes, where, "must be an existing node id"):
                check(graph.node_change.get(name) != hidden, where,
                      f"a {when} step cannot use a node that was {hidden}")
    edge = step.get("edge")
    if edge is not None:
        if check(isinstance(edge, str) and edge in graph.edges, join(path, "edge"), "must be an existing edge id"):
            record = graph.edges[edge]
            check(graph.edge_change.get(edge) != hidden, join(path, "edge"),
                  f"a {when} step cannot use an edge that was {hidden}")
            if isinstance(members, list):
                check(record.get("source") in members and record.get("target") in members, join(path, "edge"),
                      "both endpoints of the edge must be in the step's nodes")


def validate_v2(document, head: str, snapshot) -> None:
    """Raise ArchitectureError listing every contract violation, or return None when valid."""
    errors: list[dict] = []

    def check(condition, path, reason) -> bool:
        if not condition:
            errors.append({"path": path, "reason": reason})
        return bool(condition)

    if not check(isinstance(document, dict), "$", "must be a JSON object"):
        raise ArchitectureError(errors)
    index = SnapshotIndex(snapshot)
    try:
        size = len(json.dumps(document, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        size = None
    check(size is not None, "$", "must be serializable as JSON")
    check(size is None or size <= MAX_BYTES, "$", f"serialized document exceeds {MAX_BYTES} bytes")

    for key in TOP_KEYS:
        check(key in document, key, "missing")
    for key in sorted(set(document) - set(ALL_TOP_KEYS), key=str):
        check(False, str(key)[:64], "unexpected key")
    check(is_int(document.get("schema_version")) and document["schema_version"] == SCHEMA_VERSION,
          "schema_version", f"must be the integer {SCHEMA_VERSION}")
    check(one_of(document.get("generated_by"), GENERATED_BY), "generated_by", f"must be one of {list(GENERATED_BY)}")
    check(isinstance(head, str) and HEAD_PATTERN.fullmatch(head) is not None and document.get("head_sha") == head,
          "head_sha", "must equal the current head SHA")
    check(snapshot_binds_head(index.pr, head), "snapshot.pr.commits", "captured commits must include the current head")
    check(is_text(document.get("title"), 160), "title", "must be 1-160 characters")
    check(is_text(document.get("description"), 1200), "description", "must be 1-1200 characters")
    gaps = document.get("gaps", [])
    if check(isinstance(gaps, list) and len(gaps) <= MAX_GAPS, "gaps", f"must be a list of at most {MAX_GAPS}"):
        for position, gap in enumerate(gaps):
            check(is_text(gap, 300), f"gaps[{position}]", "must be 1-300 characters")

    graph = check_graph(document, index, check)
    steps = document.get("steps", [])
    if check(isinstance(steps, list) and len(steps) <= MAX_STEPS, "steps", f"must be a list of at most {MAX_STEPS}"):
        seen: set[str] = set()
        for position, step in enumerate(steps):
            check_step(step, f"steps[{position}]", graph, check)
            name = step.get("id") if isinstance(step, dict) else None
            if isinstance(name, str):
                check(name not in seen, f"steps[{position}].id", "duplicate id")
                seen.add(name)
    if "overview" in document and not errors:
        check_attached_overview(document, head, snapshot, check)
    if errors:
        raise ArchitectureError(errors)


def check_attached_overview(document: dict, head: str, snapshot, check) -> None:
    """Validate the optional `overview` with the overview module, which recomputes its computed fields.

    Only runs once the detailed graph is valid, since the overview is checked against it. Its error
    paths already start with `overview`; a document-level path is reported as `overview`.
    """
    try:
        from paircli_overview import validate_overview
        detailed = {key: value for key, value in document.items() if key != "overview"}
        validate_overview(document["overview"], detailed, head, snapshot)
    except ArchitectureError as error:
        for item in error.errors:
            path = item.get("path") if isinstance(item, dict) else None
            path = "overview" if path in (None, "$") else str(path)
            check(False, path, item.get("reason", "invalid") if isinstance(item, dict) else "invalid")
    except Exception as error:
        check(False, "overview", f"could not be validated ({type(error).__name__})")


# --- drop invalid steps and the pipeline -------------------------------------------------

def drop_invalid_steps(document, head: str, snapshot):
    """Remove steps that fail in isolation; the graph is untouched. Returns `(document, dropped)`.

    Each dropped entry is `{"id": step id or None, "errors": [{path, reason}]}`. A step that
    repeats an earlier kept id, or exceeds the step limit, is dropped too, so a document with
    a valid graph validates afterwards.
    """
    if not isinstance(document, dict) or "steps" not in document:
        return document, []
    graph = check_graph(document, SnapshotIndex(snapshot), silent)
    steps = document["steps"]
    if not isinstance(steps, list):
        result = dict(document, steps=[])
        return result, [{"id": None, "errors": [{"path": "steps", "reason": "must be a list"}]}]
    kept, dropped, seen = [], [], set()
    for position, step in enumerate(steps):
        errors: list[dict] = []

        def check(condition, path, reason) -> bool:
            if not condition:
                errors.append({"path": path, "reason": reason})
            return bool(condition)

        path = f"steps[{position}]"
        check_step(step, path, graph, check)
        name = step.get("id") if isinstance(step, dict) and isinstance(step.get("id"), str) else None
        check(name is None or name not in seen, join(path, "id"), "duplicate id")
        check(len(kept) < MAX_STEPS, path, f"more than {MAX_STEPS} steps")
        if errors:
            dropped.append({"id": name, "errors": errors})
        else:
            kept.append(step)
            seen.add(name)
    return dict(document, steps=kept), dropped


def finalize_v2(draft, head: str, snapshot, generated_by: str):
    """normalize -> enrich -> drop invalid steps -> validate. Returns `(document, warnings)`."""
    document, warnings = normalize_v2(draft)
    document = enrich_v2(document, head, snapshot, generated_by, warnings)
    document, dropped = drop_invalid_steps(document, head, snapshot)
    for entry in dropped:
        shown = "; ".join(f"{error['path']}: {error['reason']}" for error in entry["errors"][:3])
        warnings.append(f"step {entry['id']} dropped: {shown}")
    validate_v2(document, head, snapshot)
    return document, warnings
