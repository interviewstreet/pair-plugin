import copy
import json
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from paircli_architecture import MAX_BYTES, ArchitectureError
from paircli_architecture_v2 import (
    compute_layout, drop_invalid_steps, enrich_v2, finalize_v2, normalize_v2, validate_v2,
)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
KIND = {" ": "ctx", "+": "add", "-": "del"}


# --- local snapshot helpers (this file does not depend on tests/snapshot_factory.py) ---------

def hunk(old_start, new_start, pattern):
    """One hunk from a pattern: ' ' context, '+' added, '-' removed lines."""
    return {"old_start": old_start, "old_lines": sum(c != "+" for c in pattern),
            "new_start": new_start, "new_lines": sum(c != "-" for c in pattern),
            "lines": [{"kind": KIND[c], "text": "x"} for c in pattern]}


def changed(path, status="modified", hunks=None, old_path=None):
    entry = {"path": path, "status": status, "hunks": hunks if hunks is not None else []}
    if old_path:
        entry["old_path"] = old_path
    return entry


def snapshot(files=None, commits=None):
    return {"pr": {"commits": commits if commits is not None else [{"sha": HEAD}], "files": files or FILES()}}


def FILES():
    """new-side lines of reviews.ts: 10 ctx, 11 ctx, 12 add, 13 add, 14 ctx, 15 ctx; old side: 12 is del."""
    return [
        changed("src/api/reviews.ts", hunks=[hunk(10, 10, "  ++-  ")]),
        changed("src/api/session.ts", "added", [hunk(0, 1, "+++++")]),
        changed("src/db/store.ts", hunks=[hunk(1, 1, "+  ")]),
        changed("src/db/legacy.sql", "deleted", [hunk(1, 0, "----")]),
        changed("src/lib/names.ts", "renamed", [hunk(5, 5, " +- ")], old_path="src/util/names.ts"),
        changed("docs/notes.md"),  # no hunks: nothing in it can be cited by line
        changed("package-lock.json", hunks=[hunk(1, 1, " +")]),
    ]


def ref(path, start, end=None, side="new"):
    return {"type": "diff", "path": path, "start": start, "end": start if end is None else end, "side": side}


REVIEWS, SESSION, STORE, LEGACY, NAMES = ("src/api/reviews.ts", "src/api/session.ts", "src/db/store.ts",
                                          "src/db/legacy.sql", "src/lib/names.ts")


def node(name, group, kind, files=(), evidence=(), description="what changed"):
    return {"id": name, "label": name, "group": group, "kind": kind, "description": description,
            "files": list(files), "evidence": list(evidence)}


def edge(name, source, target, evidence):
    return {"id": name, "source": source, "target": target, "label": "calls",
            "claim": f"{source} uses {target}.", "evidence": list(evidence)}


def draft():
    """A model-style draft: no computed fields, no schema_version/head_sha/generated_by."""
    return {
        "title": "Reviews are validated", "description": "The API validates a review before saving it.",
        "groups": [{"id": "api", "label": "API"}, {"id": "db", "label": "Database"}, {"id": "lib", "label": "Library"}],
        "nodes": [
            node("reviews-ts", "api", "service", [REVIEWS], [ref(REVIEWS, 12, 13)]),
            node("session-ts", "api", "service", [SESSION], [ref(SESSION, 1, 5)]),
            node("store-ts", "db", "service", [STORE], [ref(STORE, 1)]),
            node("legacy-sql", "db", "database", [LEGACY], [ref(LEGACY, 1, 4, "old")]),
            node("pg", "db", "external"),
            node("tooling", "api", "routine"),
            node("names-ts", "lib", "file", [NAMES], [ref("src/util/names.ts", 5, 6, "old")]),
        ],
        "edges": [
            edge("e1", "reviews-ts", "store-ts", [ref(REVIEWS, 11, 12)]),
            edge("e2", "store-ts", "legacy-sql", [ref(LEGACY, 1, 2, "old")]),
            edge("e3", "reviews-ts", "session-ts", [ref(SESSION, 1, 3)]),
            edge("e4", "store-ts", "pg", [ref(STORE, 1, 2)]),
            edge("e5", "reviews-ts", "names-ts", [ref(REVIEWS, 14, 15)]),
        ],
        "steps": [
            {"id": "s1", "when": "after", "edge": "e1", "nodes": ["reviews-ts", "store-ts"],
             "text": "The API now validates a review before saving it.", "flags": [],
             "evidence": [ref(REVIEWS, 12)]},
            {"id": "s2", "when": "before", "edge": "e2", "nodes": ["store-ts", "legacy-sql"],
             "text": "Before, the store also wrote to the legacy table.", "flags": [],
             "evidence": [ref(LEGACY, 1, 2, "old")]},
        ],
        "gaps": ["Runtime behavior was not exercised."],
    }


def valid():
    document, _warnings = finalize_v2(draft(), HEAD, snapshot(), "llm")
    return document


def errors_of(document, head=HEAD, snap="default"):
    with pytest.raises(ArchitectureError) as caught:
        validate_v2(document, head, snapshot() if snap == "default" else snap)
    return caught.value.errors


def paths_of(document, head=HEAD, snap="default"):
    return [error["path"] for error in errors_of(document, head, snap)]


# --- golden document ------------------------------------------------------------------------

def test_golden_document_finalizes_and_validates():
    document, warnings = finalize_v2(draft(), HEAD, snapshot(), "llm")
    assert warnings == []
    assert validate_v2(document, HEAD, snapshot()) is None
    assert (document["schema_version"], document["generated_by"], document["head_sha"]) == (2, "llm", HEAD)
    assert list(document) == ["schema_version", "generated_by", "head_sha", "title", "description",
                              "groups", "nodes", "edges", "steps", "gaps"]
    assert [step["flags"] for step in document["steps"]] == [[], []]
    json.dumps(document)


def test_golden_computed_fields():
    document = valid()
    nodes = {item["id"]: item for item in document["nodes"]}
    assert {name: (item["change"], item["state"], item["stats"]) for name, item in nodes.items()} == {
        "reviews-ts": ("modified", "changed", {"files": 1, "additions": 2, "deletions": 1}),
        "session-ts": ("added", "changed", {"files": 1, "additions": 5, "deletions": 0}),
        "store-ts": ("modified", "changed", {"files": 1, "additions": 1, "deletions": 0}),
        "legacy-sql": ("removed", "changed", {"files": 1, "additions": 0, "deletions": 4}),
        "pg": ("unchanged", "context", {"files": 0, "additions": 0, "deletions": 0}),
        "tooling": ("unchanged", "routine", {"files": 0, "additions": 0, "deletions": 0}),
        "names-ts": ("modified", "changed", {"files": 1, "additions": 1, "deletions": 1}),
    }
    assert {item["id"]: item["change"] for item in document["edges"]} == {
        "e1": "modified", "e2": "removed", "e3": "added", "e4": "modified", "e5": "modified"}
    assert document["groups"] == [
        {"id": "api", "label": "API", "path": "src/api", "column": 0, "order": 0},
        {"id": "db", "label": "Database", "path": "src/db", "column": 1, "order": 0},
        {"id": "lib", "label": "Library", "path": "src/lib", "column": 1, "order": 1},
    ]


def test_deterministic_generated_by_is_kept():
    document, _ = finalize_v2(draft(), HEAD, snapshot(), "deterministic")
    assert document["generated_by"] == "deterministic"


@pytest.mark.parametrize("value", ["", "human", None, 1, True])
def test_unknown_generated_by_argument_is_rejected(value):
    with pytest.raises(ArchitectureError):
        enrich_v2(draft(), HEAD, snapshot(), value)
    with pytest.raises(ArchitectureError):
        finalize_v2(draft(), HEAD, snapshot(), value)


def test_finalize_twice_is_identical_and_ignores_key_order():
    first, _ = finalize_v2(draft(), HEAD, snapshot(), "llm")
    second, _ = finalize_v2(draft(), HEAD, snapshot(), "llm")
    assert json.dumps(first) == json.dumps(second)

    def shuffled(value):
        if isinstance(value, dict):
            items = list(value.items())
            random.Random(7).shuffle(items)
            return {key: shuffled(item) for key, item in items}
        return [shuffled(item) for item in value] if isinstance(value, list) else value

    third, _ = finalize_v2(shuffled(draft()), HEAD, snapshot(), "llm")
    assert json.dumps(first) == json.dumps(third)


def test_inputs_are_not_mutated():
    source, snap = draft(), snapshot()
    before = (copy.deepcopy(source), copy.deepcopy(snap))
    finalize_v2(source, HEAD, snap, "llm")
    assert (source, snap) == before


# --- normalize ------------------------------------------------------------------------------

def test_normalize_strips_unknown_keys_at_every_level_with_one_warning_each():
    source = draft()
    source["extra"] = 1
    source["groups"][0]["color"] = "red"
    source["nodes"][0]["layer"] = 2
    source["nodes"][0]["stats"] = {"files": 1, "additions": 2, "deletions": 1, "lines": 3}
    source["nodes"][0]["evidence"][0]["note"] = "x"
    source["edges"][0]["episode_ids"] = []
    source["steps"][0]["extra"] = True
    source["steps"][0]["evidence"][0]["extra"] = True
    cleaned, warnings = normalize_v2(source)
    assert sorted(warnings) == sorted(f"{path}: removed unexpected key" for path in (
        "extra", "groups[0].color", "nodes[0].layer", "nodes[0].stats.lines", "nodes[0].evidence[0].note",
        "edges[0].episode_ids", "steps[0].extra", "steps[0].evidence[0].extra"))
    assert "extra" not in cleaned and "layer" not in cleaned["nodes"][0]
    assert "lines" not in cleaned["nodes"][0]["stats"]
    assert source["extra"] == 1  # the input is left alone


def test_normalize_defaults_only_side_and_flags():
    source = draft()
    del source["nodes"][0]["evidence"][0]["side"]
    del source["steps"][0]["flags"]
    del source["steps"][0]["edge"]
    del source["gaps"]
    del source["edges"][0]["claim"]
    cleaned, warnings = normalize_v2(source)
    assert warnings == []
    assert cleaned["nodes"][0]["evidence"][0]["side"] == "new"
    assert cleaned["steps"][0]["flags"] == []
    assert "edge" not in cleaned["steps"][0] and "gaps" not in cleaned and "claim" not in cleaned["edges"][0]


def test_normalize_keeps_explicit_side_old():
    cleaned, _ = normalize_v2(draft())
    assert cleaned["nodes"][3]["evidence"][0]["side"] == "old"


@pytest.mark.parametrize("value", [None, [], "text", 3, [1, 2]])
def test_normalize_does_not_raise_on_non_dict(value):
    cleaned, warnings = normalize_v2(value)
    assert cleaned == value and len(warnings) == 1
    assert paths_of(cleaned) == ["$"]


def test_normalize_tolerates_malformed_members():
    source = {"groups": "no", "nodes": [1, None, {"evidence": "x", "stats": 4}], "edges": {}, "steps": [["a"]]}
    cleaned, warnings = normalize_v2(source)
    assert cleaned["nodes"][:2] == [1, None] and cleaned["groups"] == "no" and warnings == []


def test_unknown_keys_are_rejected_by_direct_validation():
    document = valid()
    document["extra"] = 1
    assert "extra" in paths_of(document)
    document = valid()
    document["nodes"][0]["layer"] = 1
    assert "nodes[0].layer" in paths_of(document)


# --- enrich ---------------------------------------------------------------------------------

def test_model_supplied_computed_values_are_overwritten():
    source = draft()
    source.update({"schema_version": 9, "head_sha": OTHER_HEAD, "generated_by": "deterministic"})
    source["groups"][0].update({"path": "nope", "column": 5, "order": 9})
    source["nodes"][0].update({"change": "removed", "state": "context", "stats": {"files": 9, "additions": 9, "deletions": 9}})
    source["edges"][0]["change"] = "added"
    source["steps"][0]["flags"] = ["x"]
    expected = valid()
    enriched = enrich_v2(source, HEAD, snapshot(), "llm")
    assert enriched == expected


def test_groups_without_nodes_are_dropped_and_undeclared_ones_not_invented():
    source = draft()
    source["groups"].append({"id": "unused", "label": "Unused"})
    assert [group["id"] for group in enrich_v2(source, HEAD, snapshot())["groups"]] == ["api", "db", "lib"]
    source = draft()
    source["nodes"][0]["group"] = "ghost"
    enriched = enrich_v2(source, HEAD, snapshot())
    assert [group["id"] for group in enriched["groups"]] == ["api", "db", "lib"]
    with pytest.raises(ArchitectureError) as caught:
        finalize_v2(source, HEAD, snapshot(), "llm")
    assert "nodes[0].group" in [error["path"] for error in caught.value.errors]


@pytest.mark.parametrize("malformed", [
    None, [], "x", {}, {"nodes": "x"}, {"nodes": [1, None, {"files": "x"}, {"files": [1, None, {}]}]},
    {"groups": [1, {"id": []}, {"id": "a"}], "nodes": [{"group": []}], "edges": [{"source": [], "target": {}}]},
    {"edges": [None, {"evidence": "x"}, {"evidence": [1, {"start": "a"}, {"path": []}]}], "steps": [1, {"flags": 3}]},
])
def test_enrich_does_not_crash_on_malformed_drafts(malformed):
    result = enrich_v2(malformed, HEAD, snapshot())
    if isinstance(malformed, dict):
        assert result["schema_version"] == 2 and result["head_sha"] == HEAD
        with pytest.raises(ArchitectureError):
            validate_v2(result, HEAD, snapshot())
    else:
        assert result == malformed


@pytest.mark.parametrize("name, files, kind, expected", [
    ("added", [SESSION], "service", ("added", "changed", {"files": 1, "additions": 5, "deletions": 0})),
    ("added file kind", [SESSION], "file", ("added", "changed", {"files": 1, "additions": 5, "deletions": 0})),
    ("removed", [LEGACY], "database", ("removed", "changed", {"files": 1, "additions": 0, "deletions": 4})),
    ("modified", [REVIEWS], "service", ("modified", "changed", {"files": 1, "additions": 2, "deletions": 1})),
    ("renamed", [NAMES], "file", ("modified", "changed", {"files": 1, "additions": 1, "deletions": 1})),
    ("added and deleted", [SESSION, LEGACY], "file", ("modified", "changed", {"files": 2, "additions": 5, "deletions": 4})),
    ("added and modified", [SESSION, STORE], "file", ("modified", "changed", {"files": 2, "additions": 6, "deletions": 0})),
    ("hunk-less file", ["docs/notes.md"], "file", ("modified", "changed", {"files": 1, "additions": 0, "deletions": 0})),
    ("unchanged context", [], "service", ("unchanged", "context", {"files": 0, "additions": 0, "deletions": 0})),
    ("unchanged routine", [], "routine", ("unchanged", "routine", {"files": 0, "additions": 0, "deletions": 0})),
    # The contract lists `changed` before `routine`, so a routine node that has diff files is `changed`.
    ("changed routine", ["package-lock.json"], "routine", ("modified", "changed", {"files": 1, "additions": 1, "deletions": 0})),
    ("file outside the PR is ignored", ["elsewhere.ts"], "service", ("unchanged", "context", {"files": 0, "additions": 0, "deletions": 0})),
    ("repeated file counts once", [REVIEWS, REVIEWS], "service", ("modified", "changed", {"files": 1, "additions": 2, "deletions": 1})),
])
def test_node_change_state_and_stats(name, files, kind, expected):
    source = {"nodes": [node("n", "g", kind, files)]}
    computed = enrich_v2(source, HEAD, snapshot())["nodes"][0]
    assert (computed["change"], computed["state"], computed["stats"]) == expected


@pytest.mark.parametrize("name, refs, expected", [
    ("old side only", [ref(REVIEWS, 12, 12, "old")], "removed"),
    ("old side only, several refs", [ref(REVIEWS, 12, 12, "old"), ref(LEGACY, 1, 2, "old")], "removed"),
    ("all added lines", [ref(REVIEWS, 12, 13)], "added"),
    ("added file", [ref(SESSION, 1, 5)], "added"),
    ("added and context", [ref(REVIEWS, 11, 12)], "modified"),
    ("context only", [ref(REVIEWS, 14, 15)], "modified"),
    ("new and old together", [ref(REVIEWS, 12, 13), ref(REVIEWS, 12, 12, "old")], "modified"),
    ("no citations", [], "unchanged"),
    ("only invalid citations", [ref(REVIEWS, 100, 101)], "unchanged"),
    ("invalid ones are skipped", [ref(REVIEWS, 100), ref(REVIEWS, 12, 13)], "added"),
])
def test_edge_change_from_cited_lines(name, refs, expected):
    source = {"edges": [edge("e", "a", "b", refs)]}
    assert enrich_v2(source, HEAD, snapshot())["edges"][0]["change"] == expected


def test_group_path_is_the_common_directory_prefix():
    def paths(files_by_node):
        nodes = [node(f"n{index}", "g", "file", files) for index, files in enumerate(files_by_node)]
        return compute_layout([{"id": "g"}], nodes, [])["g"]["path"]

    assert paths([["src/a/x.ts"], ["src/a/b/y.ts"]]) == "src/a"
    assert paths([["src/a/x.ts"]]) == "src/a"
    assert paths([["a/x.ts"], ["b/y.ts"]]) == ""
    assert paths([["root.ts"], ["src/z.ts"]]) == ""
    assert paths([["src/ab/x.ts"], ["src/a/y.ts"]]) == "src"
    assert paths([[]]) == "" and paths([]) == ""


def group_layout(order, edges):
    """Layout of one single-file node per group, with `edges` between groups."""
    groups = [{"id": name} for name in order]
    nodes = [node(f"n-{name}", name, "file", [f"{name}/x.ts"]) for name in order]
    links = [edge(f"e{index}", f"n-{source}", f"n-{target}", []) for index, (source, target) in enumerate(edges)]
    layout = compute_layout(groups, nodes, links)
    return {name: (layout[name]["column"], layout[name]["order"]) for name in order}


def test_layout_chain():
    assert group_layout("abc", ["ab", "bc"]) == {"a": (0, 0), "b": (1, 0), "c": (2, 0)}


def test_layout_diamond_uses_the_longest_path():
    layout = group_layout("abcd", ["ab", "ac", "bd", "cd", "ad"])
    assert layout == {"a": (0, 0), "b": (1, 0), "c": (1, 1), "d": (2, 0)}


def test_layout_ignores_self_edges_and_unconnected_groups_sit_in_column_zero():
    assert group_layout("ab", ["aa"]) == {"a": (0, 0), "b": (0, 1)}


@pytest.mark.parametrize("edges", [["ab", "ba"], ["ba", "ab"]])
def test_layout_cycle_is_broken_deterministically(edges):
    assert group_layout("ab", edges) == {"a": (0, 0), "b": (1, 0)}
    assert group_layout("ba", edges) == {"a": (0, 0), "b": (1, 0)}


def test_layout_longer_cycle_is_stable_across_input_orders():
    edges = ["ab", "bc", "ca", "cd"]
    expected = group_layout("abcd", edges)
    assert expected == {"a": (0, 0), "b": (1, 0), "c": (2, 0), "d": (3, 0)}
    rng = random.Random(3)
    for _ in range(10):
        order, shuffled = list("abcd"), list(edges)
        rng.shuffle(order), rng.shuffle(shuffled)
        assert group_layout(order, shuffled) == expected


def test_layout_column_is_capped_at_eight_and_order_is_sorted_by_path_then_id():
    names = "abcdefghijkl"
    layout = group_layout(names, [pair for pair in zip(names, names[1:])])
    assert [layout[name][0] for name in names] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 8, 8, 8]
    assert [layout[name][1] for name in names[8:]] == [0, 1, 2, 3]
    assert layout["i"][1] < layout["j"][1] < layout["k"][1]


def test_layout_order_uses_path_before_id():
    groups = [{"id": "a"}, {"id": "b"}]
    nodes = [node("na", "a", "file", ["zzz/x.ts"]), node("nb", "b", "file", ["aaa/x.ts"])]
    layout = compute_layout(groups, nodes, [])
    assert (layout["b"]["order"], layout["a"]["order"]) == (0, 1)


def test_layout_deep_group_chain_does_not_overflow_the_stack():
    names = [f"g{index}" for index in range(5000)]
    groups = [{"id": name} for name in names]
    nodes = [node(f"n{index}", name, "file", [f"{name}/x.ts"]) for index, name in enumerate(names)]
    links = [edge(f"e{index}", f"n{index}", f"n{index + 1}", []) for index in range(len(names) - 1)]
    assert max(item["column"] for item in compute_layout(groups, nodes, links).values()) == 8


# --- direct validation faults on an enriched document ---------------------------------------

def assign(key_path, value):
    def apply(document):
        target = document
        for key in key_path[:-1]:
            target = target[key]
        target[key_path[-1]] = value
    return apply


def remove(*key_path):
    def apply(document):
        target = document
        for key in key_path[:-1]:
            target = target[key]
        del target[key_path[-1]]
    return apply


def pad_to_cap(document):
    document["unexpected_padding"] = "x" * MAX_BYTES


GRAPH_FAULTS = [
    # top level
    ("schema_version 1", assign(("schema_version",), 1), "schema_version"),
    ("schema_version bool", assign(("schema_version",), True), "schema_version"),
    ("schema_version string", assign(("schema_version",), "2"), "schema_version"),
    ("schema_version float", assign(("schema_version",), 2.0), "schema_version"),
    ("generated_by unknown", assign(("generated_by",), "human"), "generated_by"),
    ("head_sha differs", assign(("head_sha",), OTHER_HEAD), "head_sha"),
    ("head_sha missing", remove("head_sha"), "head_sha"),
    ("missing top key", remove("gaps"), "gaps"),
    ("unknown top key", assign(("extra",), 1), "extra"),
    ("title empty", assign(("title",), ""), "title"),
    ("title too long", assign(("title",), "t" * 161), "title"),
    ("description too long", assign(("description",), "d" * 1201), "description"),
    ("gap too long", assign(("gaps",), ["g" * 301]), "gaps[0]"),
    ("gap not a string", assign(("gaps",), [5]), "gaps[0]"),
    ("too many gaps", assign(("gaps",), ["g"] * 6), "gaps"),
    ("512 KiB cap", pad_to_cap, "$"),
    # unknown keys at each level
    ("unknown group key", assign(("groups", 0, "color"), 1), "groups[0].color"),
    ("unknown node key", assign(("nodes", 0, "layer"), 1), "nodes[0].layer"),
    ("unknown edge key", assign(("edges", 0, "episode_ids"), []), "edges[0].episode_ids"),
    ("unknown step key", assign(("steps", 0, "extra"), 1), "steps[0].extra"),
    ("unknown evidence key", assign(("nodes", 0, "evidence", 0, "note"), 1), "nodes[0].evidence[0].note"),
    ("missing evidence side", remove("nodes", 0, "evidence", 0, "side"), "nodes[0].evidence[0].side"),
    ("missing step flags", remove("steps", 0, "flags"), "steps[0].flags"),
    ("missing node stats", remove("nodes", 0, "stats"), "nodes[0].stats"),
    ("unknown stats key", assign(("nodes", 0, "stats", "lines"), 1), "nodes[0].stats"),
    # ids
    ("duplicate group id", assign(("groups", 1, "id"), "api"), "groups[1].id"),
    ("duplicate node id", assign(("nodes", 1, "id"), "reviews-ts"), "nodes[1].id"),
    ("duplicate edge id", assign(("edges", 1, "id"), "e1"), "edges[1].id"),
    ("duplicate step id", assign(("steps", 1, "id"), "s1"), "steps[1].id"),
    ("id starts with digit", assign(("nodes", 0, "id"), "1a"), "nodes[0].id"),
    ("id too long", assign(("nodes", 0, "id"), "a" * 65), "nodes[0].id"),
    ("id with space", assign(("edges", 0, "id"), "e 1"), "edges[0].id"),
    ("id not a string", assign(("steps", 0, "id"), 5), "steps[0].id"),
    # references
    ("node group undeclared", assign(("nodes", 0, "group"), "ghost"), "nodes[0].group"),
    ("node group not a string", assign(("nodes", 0, "group"), ["api"]), "nodes[0].group"),
    ("edge source missing", assign(("edges", 0, "source"), "ghost"), "edges[0].source"),
    ("edge target missing", assign(("edges", 0, "target"), "ghost"), "edges[0].target"),
    ("edge endpoint unhashable", assign(("edges", 0, "target"), ["x"]), "edges[0].target"),
    ("self edge", assign(("edges", 0, "target"), "reviews-ts"), "edges[0]"),
    ("node kind unknown", assign(("nodes", 0, "kind"), "layer"), "nodes[0].kind"),
    ("group with no nodes", lambda d: d["groups"].append({"id": "empty", "label": "E", "path": "", "column": 0, "order": 3}),
     "groups[3]"),
    # text and limits
    ("node label too long", assign(("nodes", 0, "label"), "l" * 81), "nodes[0].label"),
    ("node description empty", assign(("nodes", 0, "description"), ""), "nodes[0].description"),
    ("edge label empty", assign(("edges", 0, "label"), ""), "edges[0].label"),
    ("edge claim too long", assign(("edges", 0, "claim"), "c" * 401), "edges[0].claim"),
    ("group label too long", assign(("groups", 0, "label"), "g" * 81), "groups[0].label"),
    ("13 groups", lambda d: d.update(groups=d["groups"] * 5), "groups"),
    ("61 nodes", lambda d: d.update(nodes=d["nodes"] * 9), "nodes"),
    ("61 edges", lambda d: d.update(edges=d["edges"] * 13), "edges"),
    ("25 steps", lambda d: d.update(steps=d["steps"] * 13), "steps"),
    ("16 files on a node", lambda d: d["nodes"][0].update(files=[f"f{index}.ts" for index in range(16)]), "nodes[0].files"),
    ("13 refs on a node", lambda d: d["nodes"][0].update(evidence=[ref(REVIEWS, 12)] * 13), "nodes[0].evidence"),
    ("13 refs on an edge", lambda d: d["edges"][0].update(evidence=[ref(REVIEWS, 12)] * 13), "edges[0].evidence"),
    ("no nodes", lambda d: d.update(nodes=[], edges=[], steps=[]), "nodes"),
    ("file listed twice", lambda d: d["nodes"][0]["files"].append(REVIEWS), "nodes[0].files[1]"),
    # citations
    ("node without evidence", assign(("nodes", 0, "evidence"), []), "nodes[0].evidence"),
    ("edge without evidence", assign(("edges", 0, "evidence"), []), "edges[0].evidence"),
    ("step without evidence", assign(("steps", 0, "evidence"), []), "steps[0].evidence"),
    ("evidence not a list", assign(("edges", 0, "evidence"), "x"), "edges[0].evidence"),
    ("evidence ref not an object", assign(("edges", 0, "evidence"), ["x"]), "edges[0].evidence[0]"),
    ("new-side line outside any hunk", assign(("edges", 0, "evidence", 0, "start"), 100), "edges[0].evidence[0].start"),
    ("range straddles the hunk end", assign(("edges", 0, "evidence", 0, "end"), 16), "edges[0].evidence[0].start"),
    ("new-side line inside an old-only range", lambda d: d["edges"][0]["evidence"][0].update(start=16, end=16),
     "edges[0].evidence[0].start"),
    ("old-side line outside any hunk", lambda d: d["edges"][1]["evidence"][0].update(start=9, end=9),
     "edges[1].evidence[0].start"),
    ("old-side range past the old hunk", lambda d: d["edges"][1]["evidence"][0].update(start=1, end=5),
     "edges[1].evidence[0].start"),
    ("old-side citation of an added file", lambda d: d["edges"][2]["evidence"][0].update(side="old"),
     "edges[2].evidence[0].start"),
    ("new-side citation of a deleted file", lambda d: d["edges"][1]["evidence"][0].update(side="new"),
     "edges[1].evidence[0].start"),
    ("new-side citation by the old path of a rename",
     lambda d: d["nodes"][6]["evidence"][0].update(side="new"), "nodes[6].evidence[0].path"),
    ("old-side citation by an unknown old path",
     lambda d: d["nodes"][6]["evidence"][0].update(path="src/util/other.ts"), "nodes[6].evidence[0].path"),
    ("evidence path not in the PR", assign(("edges", 0, "evidence", 0, "path"), "src/other.ts"), "edges[0].evidence[0].path"),
    ("evidence path empty", assign(("edges", 0, "evidence", 0, "path"), ""), "edges[0].evidence[0].path"),
    ("evidence type wrong", assign(("edges", 0, "evidence", 0, "type"), "file"), "edges[0].evidence[0].type"),
    ("evidence side wrong", assign(("edges", 0, "evidence", 0, "side"), "both"), "edges[0].evidence[0].side"),
    ("evidence side unhashable", assign(("edges", 0, "evidence", 0, "side"), ["new"]), "edges[0].evidence[0].side"),
    ("evidence start zero", assign(("edges", 0, "evidence", 0, "start"), 0), "edges[0].evidence[0].start"),
    ("evidence inverted", lambda d: d["edges"][0]["evidence"][0].update(start=13, end=12), "edges[0].evidence[0].start"),
    ("evidence start is a bool", assign(("edges", 0, "evidence", 0, "start"), True), "edges[0].evidence[0].start"),
    ("evidence end is a bool", assign(("edges", 0, "evidence", 0, "end"), True), "edges[0].evidence[0].start"),
    ("evidence start is a float", assign(("edges", 0, "evidence", 0, "start"), 12.0), "edges[0].evidence[0].start"),
    ("evidence start is a string", assign(("edges", 0, "evidence", 0, "start"), "12"), "edges[0].evidence[0].start"),
    ("citation of a hunk-less file", lambda d: d["edges"][0]["evidence"].append(ref("docs/notes.md", 1)),
     "edges[0].evidence[1].start"),
    ("files entry of a hunk-less file", lambda d: d["nodes"][0]["files"].append("docs/notes.md"),
     "nodes[0].files[1]"),
    ("files entry without a citation", lambda d: d["nodes"][0]["files"].append(STORE), "nodes[0].files[1]"),
    ("files entry outside the PR", lambda d: d["nodes"][0]["files"].append("src/other.ts"), "nodes[0].files[1]"),
    ("files entry is the old path of a rename", assign(("nodes", 6, "files"), ["src/util/names.ts"]), "nodes[6].files[0]"),
    ("rename node cites an unrelated file", assign(("nodes", 6, "evidence"), [ref(REVIEWS, 12)]), "nodes[6].files[0]"),
    ("routine kind removed, so evidence is required", assign(("nodes", 5, "kind"), "service"), "nodes[5].evidence"),
    ("unchanged node is no longer an edge endpoint", lambda d: d["edges"].pop(3), "nodes[4].evidence"),
    ("unchanged node whose edge has no valid evidence", assign(("edges", 3, "evidence"), []), "nodes[4].evidence"),
    ("changed external node may skip evidence but not file citations",
     lambda d: d["nodes"][0].update(kind="external", evidence=[]), "nodes[0].files[0]"),
    ("unchanged context node of a service kind without edge",
     lambda d: (d["nodes"][4].update(kind="service"), d["edges"].pop(3)), "nodes[4].evidence"),
    # computed fields
    ("node change edited", assign(("nodes", 0, "change"), "added"), "nodes[0].change"),
    ("node change invalid", assign(("nodes", 0, "change"), "renamed"), "nodes[0].change"),
    ("node state edited", assign(("nodes", 0, "state"), "context"), "nodes[0].state"),
    ("node stats edited", assign(("nodes", 0, "stats", "additions"), 3), "nodes[0].stats"),
    ("node stats bool", assign(("nodes", 4, "stats", "files"), False), "nodes[4].stats"),
    ("node stats not a dict", assign(("nodes", 0, "stats"), 3), "nodes[0].stats"),
    ("edge change edited", assign(("edges", 0, "change"), "added"), "edges[0].change"),
    ("group path edited", assign(("groups", 0, "path"), "src"), "groups[0].path"),
    ("group column edited", assign(("groups", 1, "column"), 0), "groups[1].column"),
    ("group order edited", assign(("groups", 2, "order"), 0), "groups[2].order"),
    ("group column is a bool", assign(("groups", 0, "column"), False), "groups[0].column"),
    ("group order is a float", assign(("groups", 0, "order"), 0.0), "groups[0].order"),
    ("group order is a bool", assign(("groups", 1, "order"), False), "groups[1].order"),
]


@pytest.mark.parametrize("name, mutate, expected", GRAPH_FAULTS, ids=[fault[0] for fault in GRAPH_FAULTS])
def test_graph_fault_is_reported_at_its_path(name, mutate, expected):
    document = valid()
    mutate(document)
    assert expected in paths_of(document)


STEP_FAULTS = [
    ("when invalid", assign(("steps", 0, "when"), "during"), "steps[0].when"),
    ("when missing", remove("steps", 0, "when"), "steps[0].when"),
    ("node missing", assign(("steps", 0, "nodes"), ["ghost"]), "steps[0].nodes[0]"),
    ("node not a string", assign(("steps", 0, "nodes"), [["reviews-ts"]]), "steps[0].nodes[0]"),
    ("nodes empty", assign(("steps", 0, "nodes"), []), "steps[0].nodes"),
    ("nodes not a list", assign(("steps", 0, "nodes"), "reviews-ts"), "steps[0].nodes"),
    ("17 nodes", assign(("steps", 0, "nodes"), ["reviews-ts", "store-ts"] * 8 + ["pg"]), "steps[0].nodes"),
    ("edge missing", assign(("steps", 0, "edge"), "ghost"), "steps[0].edge"),
    ("edge not a string", assign(("steps", 0, "edge"), 5), "steps[0].edge"),
    ("edge endpoints not in nodes", assign(("steps", 0, "nodes"), ["reviews-ts"]), "steps[0].edge"),
    ("only the source is in nodes", assign(("steps", 0, "nodes"), ["store-ts", "pg"]), "steps[0].edge"),
    ("after-step on a removed node", assign(("steps", 0, "nodes"), ["reviews-ts", "store-ts", "legacy-sql"]), "steps[0].nodes[2]"),
    ("after-step on a removed edge", lambda d: d["steps"][0].update(edge="e2", nodes=["store-ts", "legacy-sql"]), "steps[0].edge"),
    ("before-step on an added node", assign(("steps", 1, "nodes"), ["store-ts", "legacy-sql", "session-ts"]), "steps[1].nodes[2]"),
    ("before-step on an added edge", lambda d: d["steps"][1].update(edge="e3", nodes=["reviews-ts", "session-ts"]), "steps[1].edge"),
    ("text empty", assign(("steps", 0, "text"), ""), "steps[0].text"),
    ("text too long", assign(("steps", 0, "text"), "t" * 401), "steps[0].text"),
    ("flags not empty", assign(("steps", 0, "flags"), ["risky"]), "steps[0].flags"),
    ("flags not a list", assign(("steps", 0, "flags"), None), "steps[0].flags"),
    ("evidence missing", assign(("steps", 0, "evidence"), []), "steps[0].evidence"),
    ("evidence outside the hunks", assign(("steps", 0, "evidence", 0, "start"), 99), "steps[0].evidence[0].start"),
    ("step not an object", assign(("steps", 0), "s1"), "steps[0]"),
    ("unknown key", assign(("steps", 0, "extra"), 1), "steps[0].extra"),
]


@pytest.mark.parametrize("name, mutate, expected", STEP_FAULTS, ids=[fault[0] for fault in STEP_FAULTS])
def test_step_fault_is_reported_and_only_that_step_is_dropped(name, mutate, expected):
    document = valid()
    mutate(document)
    assert expected in paths_of(document)
    kept, dropped = drop_invalid_steps(document, HEAD, snapshot())
    bad = int(expected[len("steps["):].split("]")[0])
    assert [entry["errors"][0]["path"].split(".")[0] for entry in dropped] == [f"steps[{bad}]"]
    assert kept["steps"] == [document["steps"][1 - bad]]
    assert kept["nodes"] == document["nodes"] and kept["edges"] == document["edges"]
    validate_v2(kept, HEAD, snapshot())


def test_optional_step_edge_may_be_null_or_absent():
    document = valid()
    document["steps"][0]["edge"] = None
    del document["steps"][1]["edge"]
    validate_v2(document, HEAD, snapshot())


def test_step_text_of_exactly_400_characters_is_valid():
    document = valid()
    document["steps"][0]["text"] = "t" * 400
    validate_v2(document, HEAD, snapshot())


# --- head binding ---------------------------------------------------------------------------

def test_stale_head_is_rejected():
    paths = paths_of(valid(), OTHER_HEAD)
    assert "head_sha" in paths and "snapshot.pr.commits" in paths


@pytest.mark.parametrize("head", ["", "A" * 40, "a" * 39, None])
def test_malformed_head_argument_is_rejected(head):
    document = valid()
    document["head_sha"] = head
    assert "head_sha" in paths_of(document, head)


def test_snapshot_without_head_commit_is_rejected():
    assert "snapshot.pr.commits" in paths_of(valid(), HEAD, snapshot(commits=[{"sha": OTHER_HEAD}]))
    assert "snapshot.pr.commits" in paths_of(valid(), HEAD, snapshot(commits=[]))
    assert "snapshot.pr.commits" in paths_of(valid(), HEAD, {})


def test_truncated_commit_list_cannot_disprove_the_head():
    full_list = [{"sha": f"{index:040x}"} for index in range(100)]
    validate_v2(valid(), HEAD, snapshot(commits=full_list))
    assert "snapshot.pr.commits" in paths_of(valid(), HEAD, snapshot(commits=full_list[:99]))
    validate_v2(valid(), HEAD, snapshot(commits=full_list[:99] + [{"sha": HEAD}]))


# --- computed fields are guarded ------------------------------------------------------------

def test_an_unenriched_document_is_rejected():
    document = copy.deepcopy(draft())
    document.update({"schema_version": 2, "generated_by": "llm", "head_sha": HEAD})
    paths = paths_of(document)
    assert "nodes[0].change" in paths and "nodes[0].state" in paths and "groups[0].path" in paths
    assert "edges[0].change" in paths


def test_a_document_enriched_for_another_snapshot_is_rejected():
    document = valid()
    other = snapshot()
    other["pr"]["files"][0]["hunks"] = [hunk(10, 10, "  +  ")]
    assert "nodes[0].stats" in paths_of(document, HEAD, other)


# --- drop_invalid_steps and finalize --------------------------------------------------------

def test_drop_invalid_steps_keeps_the_graph_and_reports_dropped_ids():
    source = draft()
    source["steps"][0]["nodes"] = ["reviews-ts", "ghost"]
    source["steps"].append({"id": "s3", "when": "after", "nodes": ["session-ts"], "text": "Sessions exist now.",
                            "flags": [], "evidence": [ref(SESSION, 1, 2)]})
    enriched = enrich_v2(source, HEAD, snapshot())
    kept, dropped = drop_invalid_steps(enriched, HEAD, snapshot())
    assert [item["id"] for item in dropped] == ["s1"]
    assert dropped[0]["errors"][0] == {"path": "steps[0].nodes[1]", "reason": "must be an existing node id"}
    assert [item["id"] for item in kept["steps"]] == ["s2", "s3"]
    for key in ("groups", "nodes", "edges", "gaps", "title"):
        assert kept[key] == enriched[key]
    assert len(enriched["steps"]) == 3  # not modified in place
    validate_v2(kept, HEAD, snapshot())


def test_drop_invalid_steps_drops_duplicates_and_extra_steps():
    document = valid()
    clone = copy.deepcopy(document["steps"][0])
    document["steps"] = [document["steps"][0], clone]
    kept, dropped = drop_invalid_steps(document, HEAD, snapshot())
    assert len(kept["steps"]) == 1 and [item["id"] for item in dropped] == ["s1"]
    validate_v2(kept, HEAD, snapshot())

    document["steps"] = [dict(clone, id=f"s{index}") for index in range(26)]
    kept, dropped = drop_invalid_steps(document, HEAD, snapshot())
    assert len(kept["steps"]) == 24 and [item["id"] for item in dropped] == ["s24", "s25"]
    validate_v2(kept, HEAD, snapshot())


@pytest.mark.parametrize("steps", ["x", {}, None, [1, None, "s"]])
def test_drop_invalid_steps_handles_malformed_steps(steps):
    document = valid()
    document["steps"] = steps
    kept, dropped = drop_invalid_steps(document, HEAD, snapshot())
    assert kept["steps"] == [] and dropped and all(entry["id"] is None for entry in dropped)
    validate_v2(kept, HEAD, snapshot())


@pytest.mark.parametrize("value", [None, [], "x", 3])
def test_drop_invalid_steps_leaves_non_dict_documents_alone(value):
    assert drop_invalid_steps(value, HEAD, snapshot()) == (value, [])


def test_finalize_drops_bad_steps_with_warnings_and_keeps_the_graph():
    source = draft()
    source["steps"][1]["when"] = "after"  # an after-step on the removed edge e2
    document, warnings = finalize_v2(source, HEAD, snapshot(), "llm")
    assert [item["id"] for item in document["steps"]] == ["s1"]
    assert len(warnings) == 1 and warnings[0].startswith("step s2 dropped: steps[1].nodes[1]")
    assert len(document["nodes"]) == 7


def test_finalize_reports_stripped_keys():
    source = draft()
    source["nodes"][0]["layer"] = 3
    _document, warnings = finalize_v2(source, HEAD, snapshot(), "llm")
    assert warnings == ["nodes[0].layer: removed unexpected key"]


def test_finalize_raises_when_the_graph_is_invalid():
    source = draft()
    source["edges"][0]["target"] = "ghost"
    source["nodes"][1]["evidence"] = []
    with pytest.raises(ArchitectureError) as caught:
        finalize_v2(source, HEAD, snapshot(), "llm")
    assert {"edges[0].target", "nodes[1].evidence"} <= {error["path"] for error in caught.value.errors}


def test_finalize_rejects_a_stale_head():
    with pytest.raises(ArchitectureError) as caught:
        finalize_v2(draft(), OTHER_HEAD, snapshot(), "llm")
    assert "snapshot.pr.commits" in [error["path"] for error in caught.value.errors]


@pytest.mark.parametrize("value", [None, [], "x", 5])
def test_finalize_rejects_non_dict_documents_cleanly(value):
    with pytest.raises(ArchitectureError) as caught:
        finalize_v2(value, HEAD, snapshot(), "llm")
    assert caught.value.errors == [{"path": "$", "reason": "must be a JSON object"}]


# --- error shape ----------------------------------------------------------------------------

def test_errors_are_structured_and_all_collected():
    document = valid()
    document["title"] = ""
    document["nodes"][0]["group"] = "ghost"
    document["edges"][0]["evidence"] = []
    document["steps"][0]["text"] = ""
    errors = errors_of(document)
    assert all(set(error) == {"path", "reason"} and all(isinstance(value, str) for value in error.values())
               for error in errors)
    assert {"title", "nodes[0].group", "edges[0].evidence", "steps[0].text"} <= {error["path"] for error in errors}
    assert isinstance(ArchitectureError([{"path": "x", "reason": "y"}]), ValueError)


@pytest.mark.parametrize("value", [None, [], "x", 5, True])
def test_validate_rejects_non_dict_documents(value):
    assert errors_of(value) == [{"path": "$", "reason": "must be a JSON object"}]


@pytest.mark.parametrize("snap", [None, [], "x", {"pr": None}, {"pr": {"files": 5, "commits": [None, 1]}}])
def test_validate_survives_malformed_snapshots(snap):
    assert "snapshot.pr.commits" in paths_of(valid(), HEAD, snap)


def test_validate_survives_malformed_snapshot_files():
    snap = snapshot(files=[None, "x", {"path": 5}, {"path": "src/api/reviews.ts", "hunks": [None, {"new_start": "a"}]}])
    assert "nodes[0].evidence[0].start" in paths_of(valid(), HEAD, snap)


def test_validate_survives_unserializable_documents():
    document = valid()
    document["title"] = {1, 2}
    paths = paths_of(document)
    assert "$" in paths and "title" in paths


def test_validate_survives_nested_garbage():
    document = valid()
    document["nodes"] = [None, 5, [], {"id": ["x"], "files": [None, 5], "evidence": [None, {"start": []}]}]
    document["edges"] = [{"source": [], "target": {}, "evidence": [[]]}]
    document["groups"] = [1, {"id": []}]
    document["steps"] = [{"nodes": [[]], "edge": [], "evidence": "x", "when": []}]
    assert len(errors_of(document)) > 10


# --- size and speed -------------------------------------------------------------------------

def test_large_document_and_snapshot_finalize_quickly():
    files = [changed(f"src/pkg{index % 12}/file{index}.ts", hunks=[hunk(1, 1, " + -  " * 5)]) for index in range(253)]
    big = snapshot(files)
    nodes = [node(f"n{index}", f"g{index % 12}", "file", [f"src/pkg{index % 12}/file{index}.ts"],
                  [ref(f"src/pkg{index % 12}/file{index}.ts", 2, 3)]) for index in range(60)]
    edges = [edge(f"e{index}", f"n{index}", f"n{(index + 1) % 60}", [ref(f"src/pkg{index % 12}/file{index}.ts", 1, 3)])
             for index in range(60)]
    steps = [{"id": f"s{index}", "when": "after", "edge": f"e{index}", "nodes": [f"n{index}", f"n{(index + 1) % 60}"],
              "text": "Step.", "flags": [], "evidence": [ref(f"src/pkg{index % 12}/file{index}.ts", 2)]}
             for index in range(24)]
    source = {"title": "Big", "description": "Large PR.", "groups": [{"id": f"g{index}", "label": f"G{index}"} for index in range(12)],
              "nodes": nodes, "edges": edges, "steps": steps, "gaps": []}
    started = time.monotonic()
    document, warnings = finalize_v2(source, HEAD, big, "llm")
    assert time.monotonic() - started < 5
    assert warnings == [] and len(document["steps"]) == 24
    assert max(group["column"] for group in document["groups"]) <= 8
    assert len(json.dumps(document).encode()) < MAX_BYTES
