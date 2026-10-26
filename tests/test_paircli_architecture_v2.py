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
        "reviews-ts": ("modified", "changed", {"files": 1, "additions": 2, "deletions": 1, "api_added": 0, "api_removed": 0, "after_return": 0}),
        "session-ts": ("added", "changed", {"files": 1, "additions": 5, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0}),
        "store-ts": ("modified", "changed", {"files": 1, "additions": 1, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0}),
        "legacy-sql": ("removed", "changed", {"files": 1, "additions": 0, "deletions": 4, "api_added": 0, "api_removed": 0, "after_return": 0}),
        "pg": ("unchanged", "context", {"files": 0, "additions": 0, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0}),
        "tooling": ("unchanged", "routine", {"files": 0, "additions": 0, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0}),
        "names-ts": ("modified", "changed", {"files": 1, "additions": 1, "deletions": 1, "api_added": 0, "api_removed": 0, "after_return": 0}),
    }
    assert {item["id"]: item["change"] for item in document["edges"]} == {
        "e1": "modified", "e2": "removed", "e3": "added", "e4": "modified", "e5": "unchanged"}
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
    source["nodes"][0].update({"change": "removed", "state": "context", "stats": {"files": 9, "additions": 9, "deletions": 9, "api_added": 0, "api_removed": 0, "after_return": 0}})
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
    ("added", [SESSION], "service", ("added", "changed", {"files": 1, "additions": 5, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("added file kind", [SESSION], "file", ("added", "changed", {"files": 1, "additions": 5, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("removed", [LEGACY], "database", ("removed", "changed", {"files": 1, "additions": 0, "deletions": 4, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("modified", [REVIEWS], "service", ("modified", "changed", {"files": 1, "additions": 2, "deletions": 1, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("renamed", [NAMES], "file", ("modified", "changed", {"files": 1, "additions": 1, "deletions": 1, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("added and deleted", [SESSION, LEGACY], "file", ("modified", "changed", {"files": 2, "additions": 5, "deletions": 4, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("added and modified", [SESSION, STORE], "file", ("modified", "changed", {"files": 2, "additions": 6, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("hunk-less file", ["docs/notes.md"], "file", ("modified", "changed", {"files": 1, "additions": 0, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("unchanged context", [], "service", ("unchanged", "context", {"files": 0, "additions": 0, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("unchanged routine", [], "routine", ("unchanged", "routine", {"files": 0, "additions": 0, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0})),
    # A routine row is collapsed in the view even when its files changed.
    ("changed routine", ["package-lock.json"], "routine", ("modified", "routine", {"files": 1, "additions": 1, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("file outside the PR is ignored", ["elsewhere.ts"], "service", ("unchanged", "context", {"files": 0, "additions": 0, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0})),
    ("repeated file counts once", [REVIEWS, REVIEWS], "service", ("modified", "changed", {"files": 1, "additions": 2, "deletions": 1, "api_added": 0, "api_removed": 0, "after_return": 0})),
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
    ("context only", [ref(REVIEWS, 14, 15)], "unchanged"),
    ("context on the old side only", [ref(REVIEWS, 10, 11, "old")], "unchanged"),
    ("context on both sides", [ref(REVIEWS, 14, 15), ref(REVIEWS, 13, 14, "old")], "unchanged"),
    ("context plus a deleted old line", [ref(REVIEWS, 10, 12, "old")], "removed"),
    ("deleted old line plus new context", [ref(REVIEWS, 12, 12, "old"), ref(REVIEWS, 14, 14)], "modified"),
    ("added line plus old context", [ref(REVIEWS, 12, 13), ref(REVIEWS, 10, 11, "old")], "modified"),
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
    source["steps"][1]["evidence"] = [ref(REVIEWS, 12)]
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


# --- before and after steps cite the matching side -------------------------------------------

def step_with(when, evidence, **values):
    return {"id": "s1", "when": when, "nodes": ["reviews-ts"], "text": "Reviews.", "flags": [],
            "evidence": evidence, **values}


def finalize_step(step, snap=None):
    source = draft()
    source["steps"] = [step]
    return finalize_v2(source, HEAD, snap or snapshot(), "llm")


def test_a_before_step_citing_context_lines_moves_them_to_the_old_side():
    # New 14-15 are context lines that were old 13-14.
    document, warnings = finalize_step(step_with("before", [ref(REVIEWS, 14, 15)]))
    assert [step["evidence"] for step in document["steps"]] == [[ref(REVIEWS, 13, 14, "old")]]
    assert warnings == []
    assert validate_v2(document, HEAD, snapshot()) is None


def test_a_before_step_citing_an_added_line_drops_the_ref_and_the_step_when_nothing_is_left():
    document, warnings = finalize_step(step_with("before", [ref(REVIEWS, 12, 13)]))
    assert document["steps"] == []
    assert warnings[0] == ("steps[0].evidence[0]: a before step cannot cite new-side lines that were added; "
                           "citation dropped")
    assert warnings[1].startswith("step s1 dropped: steps[0].evidence: needs at least one valid diff citation")


def test_a_before_step_citing_a_range_that_mixes_context_and_added_lines_loses_that_ref_only():
    document, warnings = finalize_step(step_with("before", [ref(REVIEWS, 11, 12), ref(REVIEWS, 10, 11)]))
    assert [step["evidence"] for step in document["steps"]] == [[ref(REVIEWS, 10, 11, "old")]]
    assert len(warnings) == 1 and "evidence[0]" in warnings[0]


def test_a_before_step_keeps_old_side_refs_on_deleted_lines():
    document, warnings = finalize_step(step_with("before", [ref(REVIEWS, 12, 12, "old"), ref(REVIEWS, 14, 14)]))
    assert document["steps"][0]["evidence"] == [ref(REVIEWS, 12, 12, "old"), ref(REVIEWS, 13, 13, "old")]
    assert warnings == []


def test_an_after_step_citing_context_on_the_old_side_moves_to_the_new_side_and_deleted_lines_drop():
    document, warnings = finalize_step(step_with("after", [ref(REVIEWS, 13, 14, "old"), ref(REVIEWS, 12, 12, "old")]))
    assert document["steps"][0]["evidence"] == [ref(REVIEWS, 14, 15)]
    assert len(warnings) == 1 and "after step cannot cite old-side lines that were deleted" in warnings[0]


def test_a_context_run_that_is_not_contiguous_on_the_other_side_splits_into_several_refs():
    files = [changed("src/a.ts", hunks=[hunk(1, 1, "  - ")])]  # new 1,2,3 ctx; old 1,2 ctx, 3 del, 4 ctx
    snap = snapshot(files)
    source = {"nodes": [node("n", "g", "file", ["src/a.ts"], [ref("src/a.ts", 1)])], "steps": [
        {"id": "s1", "when": "before", "nodes": ["n"], "text": "t", "evidence": [ref("src/a.ts", 2, 3)]}]}
    steps = enrich_v2(source, HEAD, snap)["steps"]
    assert steps[0]["evidence"] == [ref("src/a.ts", 2, 2, "old"), ref("src/a.ts", 4, 4, "old")]


def test_the_renamed_file_keeps_its_cited_path_when_a_ref_changes_side():
    document, _ = finalize_step(step_with("before", [ref(NAMES, 5, 5)], nodes=["names-ts"]))
    # new 5 is context in " +- " (old 5 too)
    assert document["steps"][0]["evidence"] == [ref(NAMES, 5, 5, "old")]


@pytest.mark.parametrize("when, side, expected", [
    ("before", "new", "steps[0].evidence[0].side"),
    ("after", "old", "steps[0].evidence[0].side"),
])
def test_validation_requires_each_step_to_cite_its_own_side(when, side, expected):
    document = valid()
    document["steps"][0]["when"] = when
    document["steps"][0]["evidence"] = [ref(REVIEWS, 12, 12, side)]
    document["steps"][0]["nodes"] = ["reviews-ts", "store-ts"]
    assert expected in paths_of(document)


def test_validation_accepts_a_before_step_on_the_old_side_and_an_after_step_on_the_new_side():
    document = valid()
    assert [step["when"] for step in document["steps"]] == ["after", "before"]
    assert {ref["side"] for step in document["steps"] for ref in step["evidence"]} == {"new", "old"}
    assert validate_v2(document, HEAD, snapshot()) is None


def test_drop_invalid_steps_drops_a_wrong_side_step_with_the_reason():
    document = valid()
    document["steps"][1]["evidence"] = [ref(LEGACY, 1, 2, "new")]  # not even a valid new-side range
    document["steps"][0]["when"] = "before"                        # an unconverted new-side ref
    kept, dropped = drop_invalid_steps(document, HEAD, snapshot())
    assert [entry["id"] for entry in dropped] == ["s1", "s2"]
    assert any("must cite old-side lines" in error["reason"] for error in dropped[0]["errors"])
    assert kept["steps"] == []


# --- pattern rows and API stats -------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paircli_architecture_v2_draft import deterministic_v2_draft  # noqa: E402
from paircli_skeleton import build_skeleton  # noqa: E402
from paircli_architecture_v2 import SnapshotIndex  # noqa: E402
from paircli_patterns import verify_pattern  # noqa: E402
from snapshot_factory import deleted_templates, make_file, make_snapshot, many_files, source_lines  # noqa: E402


def pattern_world():
    """A real draft with a same-change row (4 deleted files) and a tests row, finalized and valid."""
    files = deleted_templates(4, "src/legacy/templates") + [
        make_file("src/legacy/index.ts", added=["export const ready = true;", "export function boot() {}"],
                  removed=[f"export * from './templates/t{number}';" for number in range(4)]),
        make_file("src/app/service.ts", added=source_lines(3)),
    ] + [make_file(f"src/app/__tests__/c{number}.test.ts", added=[f"it('{'xyz'[number]}', () => {{}});"]) for number in range(3)]
    snap = make_snapshot(files, head=HEAD)
    draft = deterministic_v2_draft(build_skeleton(snap, HEAD), snap, HEAD)
    document, warnings = finalize_v2(draft, HEAD, snap, "deterministic")
    assert warnings == []
    return document, snap


def pattern_index(document, family):
    return next(position for position, node in enumerate(document["nodes"])
                if node["kind"] == "pattern" and node["pattern"]["family"] == family)


def pattern_errors(mutate):
    document, snap = pattern_world()
    mutate(document)
    with pytest.raises(ArchitectureError) as caught:
        validate_v2(document, HEAD, snap)
    return [(error["path"], error["reason"]) for error in caught.value.errors]


def test_a_pattern_document_is_valid_and_its_computed_fields_cover_all_members():
    document, snap = pattern_world()
    assert validate_v2(document, HEAD, snap) is None
    node = document["nodes"][pattern_index(document, "same-change")]
    assert list(node) == ["id", "label", "group", "kind", "description", "files", "evidence", "pattern",
                          "change", "state", "stats"]
    assert (node["change"], node["state"]) == ("removed", "changed")
    assert node["stats"] == {"files": 4, "additions": 0, "deletions": 16, "api_added": 0, "api_removed": 4, "after_return": 0}
    tests = document["nodes"][pattern_index(document, "tests")]
    assert tests["change"] == "modified" and tests["stats"]["files"] == 3 and "signature" not in tests["pattern"]


def test_api_stats_are_computed_for_every_node_and_summed_over_pattern_members():
    document, _ = pattern_world()
    index = next(node for node in document["nodes"] if node["files"] == ["src/legacy/index.ts"])
    assert (index["stats"]["api_added"], index["stats"]["api_removed"]) == (2, 1)


def test_older_documents_without_api_fields_are_recomputed_by_enrich():
    document, snap = pattern_world()
    for node in document["nodes"]:
        node["stats"] = {key: node["stats"][key] for key in ("files", "additions", "deletions")}
    with pytest.raises(ArchitectureError):
        validate_v2(document, HEAD, snap)
    again, warnings = finalize_v2(document, HEAD, snap, "deterministic")
    assert warnings == [] and all("api_added" in node["stats"] for node in again["nodes"])


PATTERN_FAULTS = [
    ("signature mismatch", lambda d: d["nodes"][pattern_index(d, "same-change")]["pattern"].update(signature="0" * 16),
     "pattern.members["),
    ("signature missing", lambda d: d["nodes"][pattern_index(d, "same-change")]["pattern"].pop("signature"),
     "pattern.signature"),
    ("wrong category", lambda d: d["nodes"][pattern_index(d, "tests")]["pattern"]["members"].__setitem__(
        0, "src/app/service.ts"), "pattern.members[0]"),
    ("count mismatch", lambda d: d["nodes"][pattern_index(d, "same-change")]["pattern"].update(count=5),
     "pattern.count"),
    ("too few members", lambda d: d["nodes"][pattern_index(d, "tests")]["pattern"].update(
        members=d["nodes"][pattern_index(d, "tests")]["pattern"]["members"][:2], count=2), "pattern.members"),
    ("member is not a changed file", lambda d: d["nodes"][pattern_index(d, "tests")]["pattern"]["members"].__setitem__(
        0, "src/app/ghost.test.ts"), "pattern.members[0]"),
    ("duplicate member", lambda d: d["nodes"][pattern_index(d, "tests")]["pattern"]["members"].__setitem__(
        1, d["nodes"][pattern_index(d, "tests")]["pattern"]["members"][0]), "pattern.members[1]"),
    ("unknown family", lambda d: d["nodes"][pattern_index(d, "tests")]["pattern"].update(family="misc"), "pattern.family"),
    ("signature on a category row", lambda d: d["nodes"][pattern_index(d, "tests")]["pattern"].update(signature="a" * 16),
     "pattern.signature"),
    ("unexpected pattern key", lambda d: d["nodes"][pattern_index(d, "tests")]["pattern"].update(extra=1),
     "pattern.extra"),
    ("pattern is not an object", lambda d: d["nodes"][pattern_index(d, "tests")].update(pattern="many"), "pattern"),
    ("pattern key missing", lambda d: d["nodes"][pattern_index(d, "tests")].pop("pattern"), "pattern"),
    ("sample is not a member", lambda d: d["nodes"][pattern_index(d, "same-change")]["files"].__setitem__(
        0, "src/app/service.ts"), "files[0]"),
    ("uncited sample", lambda d: d["nodes"][pattern_index(d, "same-change")]["evidence"].clear(), "files[0]"),
    ("more than 12 samples", lambda d: d["nodes"][pattern_index(d, "same-change")]["files"].extend(
        f"x{number}.ts" for number in range(10)), "files"),
    ("stats files edited", lambda d: d["nodes"][pattern_index(d, "same-change")]["stats"].update(files=1), "stats"),
    ("api_added edited", lambda d: d["nodes"][0]["stats"].update(api_added=99), "nodes[0].stats"),
    ("api_removed missing", lambda d: d["nodes"][0]["stats"].pop("api_removed"), "nodes[0].stats"),
    ("change edited", lambda d: d["nodes"][pattern_index(d, "tests")].update(change="added"), "change"),
    ("member listed in a file row", lambda d: d["nodes"].append(
        {**copy.deepcopy(next(n for n in d["nodes"] if n["files"] == ["src/app/service.ts"])), "id": "dup",
         "files": [d["nodes"][pattern_index(d, "tests")]["pattern"]["members"][0]]}), "files[0]"),
    ("pattern key on a file row", lambda d: next(n for n in d["nodes"] if n["kind"] == "file").update(
        pattern={"family": "tests", "count": 3, "members": []}), "pattern"),
]


@pytest.mark.parametrize("name, mutate, expected", PATTERN_FAULTS, ids=[fault[0] for fault in PATTERN_FAULTS])
def test_validate_rejects_a_bad_pattern_row(name, mutate, expected):
    errors = pattern_errors(mutate)
    assert any(expected in path for path, _ in errors), errors


def test_a_member_in_two_pattern_rows_is_rejected():
    def share(document):
        first = document["nodes"][pattern_index(document, "same-change")]
        second = copy.deepcopy(document["nodes"][pattern_index(document, "tests")])
        second["id"] = "tests-two"
        second["pattern"]["members"][0] = first["pattern"]["members"][0]
        document["nodes"].append(second)
    errors = pattern_errors(share)
    assert any("already a member of" in reason for _, reason in errors), errors


def test_a_pattern_citation_outside_every_hunk_is_rejected():
    errors = pattern_errors(lambda d: d["nodes"][pattern_index(d, "tests")]["evidence"].append(
        {"type": "diff", "path": "src/app/service.ts", "start": 99, "end": 99, "side": "new"}))
    assert any("evidence" in path for path, _ in errors)


def test_normalize_keeps_pattern_on_pattern_rows_and_strips_it_elsewhere_with_a_warning():
    document, _ = pattern_world()
    row = next(position for position, node in enumerate(document["nodes"]) if node["kind"] == "file")
    document["nodes"][row]["pattern"] = {"family": "tests", "count": 3, "members": []}
    document["nodes"][pattern_index(document, "tests")]["pattern"]["extra"] = 1
    cleaned, warnings = normalize_v2(document)
    assert "pattern" not in cleaned["nodes"][row]
    kept = cleaned["nodes"][pattern_index(document, "tests")]["pattern"]
    assert "extra" not in kept and kept["family"] == "tests"
    assert f"nodes[{row}].pattern: removed from a row that is not a pattern row" in warnings
    assert f"nodes[{pattern_index(document, 'tests')}].pattern.extra: removed unexpected key" in warnings
    assert "pattern" in document["nodes"][row], "the input is left alone"


def test_enrich_overwrites_model_written_pattern_stats():
    document, snap = pattern_world()
    position = pattern_index(document, "same-change")
    document["nodes"][position]["stats"] = {"files": 1, "additions": 9, "deletions": 9}
    document["nodes"][position]["change"] = "added"
    again, _ = finalize_v2(document, HEAD, snap, "llm")
    assert again["nodes"][position]["stats"]["files"] == 4 and again["nodes"][position]["change"] == "removed"


def test_pattern_members_with_hunkless_files_still_count_in_stats_and_status():
    snap = make_snapshot(deleted_templates(3) + [make_file("src/legacy/templates/gone.ts", "deleted", no_hunks=True)])
    row = {"id": "p", "label": "3 files: deleted", "group": "g", "kind": "pattern", "description": "d",
           "files": [], "evidence": [], "pattern": {"family": "tests", "count": 3, "members": [
               "src/legacy/templates/t0.ts", "src/legacy/templates/t1.ts", "src/legacy/templates/gone.ts"]}}
    computed = enrich_v2({"nodes": [row]}, HEAD, snap)["nodes"][0]
    assert computed["stats"]["files"] == 3 and computed["change"] == "removed"


def remainder_world():
    """A real draft of 20 unlike files in one directory: 12 file rows plus one remainder row of 8, finalized and valid."""
    files = many_files(20, directories=["src/misc"], unique=True)
    snap = make_snapshot(files, head=HEAD)
    draft = deterministic_v2_draft(build_skeleton(snap, HEAD), snap, HEAD)
    document, warnings = finalize_v2(draft, HEAD, snap, "deterministic")
    assert warnings == []
    return document, snap


def remainder_row(document):
    return next(node for node in document["nodes"] if node["kind"] == "pattern" and node["pattern"]["family"] == "remainder")


def remainder_errors(mutate):
    document, snap = remainder_world()
    mutate(document)
    with pytest.raises(ArchitectureError) as caught:
        validate_v2(document, HEAD, snap)
    return [(error["path"], error["reason"]) for error in caught.value.errors]


def test_a_remainder_row_is_valid_and_its_computed_fields_cover_all_members():
    document, snap = remainder_world()
    assert validate_v2(document, HEAD, snap) is None
    row = remainder_row(document)
    assert row["label"] == "8 other changed files" and row["pattern"]["count"] == 8
    assert list(row["pattern"]) == ["family", "count", "members"], "no signature field"
    assert row["stats"]["files"] == 8 and row["change"] == "modified" and len(row["files"]) <= 12
    rowed = {name for node in document["nodes"] if node["kind"] == "file" for name in node["files"]}
    assert rowed.isdisjoint(row["pattern"]["members"]) and len(rowed) + 8 == 20


REMAINDER_FAULTS = [
    ("signature present", lambda d: remainder_row(d)["pattern"].update(signature="a" * 16), "pattern.signature"),
    ("signature empty", lambda d: remainder_row(d)["pattern"].update(signature=""), "pattern.signature"),
    ("too few members", lambda d: remainder_row(d)["pattern"].update(members=remainder_row(d)["pattern"]["members"][:2], count=2),
     "pattern.members"),
    ("unknown member", lambda d: remainder_row(d)["pattern"]["members"].__setitem__(0, "src/misc/ghost.ts"), "pattern.members[0]"),
    ("duplicate member", lambda d: remainder_row(d)["pattern"]["members"].__setitem__(1, remainder_row(d)["pattern"]["members"][0]),
     "pattern.members[1]"),
    ("count mismatch", lambda d: remainder_row(d)["pattern"].update(count=9), "pattern.count"),
    ("member also has a file row", lambda d: next(n for n in d["nodes"] if n["kind"] == "file").update(
        files=[remainder_row(d)["pattern"]["members"][0]]), "files[0]"),
    ("member also in another pattern row", lambda d: d["nodes"].append(
        {**copy.deepcopy(remainder_row(d)), "id": "second-remainder"}), "already a member of"),
]


@pytest.mark.parametrize("name, mutate, expected", REMAINDER_FAULTS, ids=[fault[0] for fault in REMAINDER_FAULTS])
def test_validate_rejects_a_bad_remainder_row(name, mutate, expected):
    errors = remainder_errors(mutate)
    assert any(expected in path or expected in reason for path, reason in errors), errors


def test_a_remainder_may_hold_up_to_500_members_and_no_more():
    files = [make_file(f"src/bulk/f{index:04d}.ts", added=[f"const unique{index} = {index};"]) for index in range(501)]
    snap = make_snapshot(files, head=HEAD)
    names = sorted(file["path"] for file in files)

    def row(members):
        return {"id": "r", "label": f"{len(members)} other changed files", "group": "g", "kind": "pattern", "description": "d",
                "files": [], "evidence": [], "pattern": {"family": "remainder", "count": len(members), "members": members}}

    assert enrich_v2({"nodes": [row(names[:500])]}, HEAD, snap)["nodes"][0]["stats"]["files"] == 500
    index = SnapshotIndex(snap)
    assert verify_pattern(row(names[:500])["pattern"], index.files) == []
    assert verify_pattern(row(names)["pattern"], index.files)


# --- the optional top-level overview ---------------------------------------------------------

from paircli_overview import deterministic_overview, finalize_overview  # noqa: E402


def overview_world():
    """A valid detailed document that carries a valid deterministic overview."""
    document, snap = pattern_world()
    overview, warnings = finalize_overview(deterministic_overview(document, snap), document, HEAD, snap, "deterministic")
    assert warnings == []
    return dict(document, overview=overview), snap


def overview_errors(mutate):
    document, snap = overview_world()
    mutate(document)
    with pytest.raises(ArchitectureError) as caught:
        validate_v2(document, HEAD, snap)
    return [(error["path"], error["reason"]) for error in caught.value.errors]


def test_a_document_with_a_valid_overview_passes_and_without_one_still_passes():
    document, snap = overview_world()
    assert validate_v2(document, HEAD, snap) is None
    assert document["overview"]["components"] and document["overview"]["groups"]
    without = {key: value for key, value in document.items() if key != "overview"}
    assert validate_v2(without, HEAD, snap) is None


def test_enrich_and_finalize_keep_the_overview_and_put_it_after_the_detailed_keys():
    document, snap = overview_world()
    again, warnings = finalize_v2(document, HEAD, snap, "deterministic")
    assert warnings == [] and list(again)[-1] == "overview" and again["overview"] == document["overview"]
    shuffled = dict(reversed(list(document.items())))
    assert list(enrich_v2(shuffled, HEAD, snap))[-2:] == ["gaps", "overview"]


def test_a_tampered_computed_field_of_the_overview_fails_with_an_overview_path():
    def tamper_stats(document):
        document["overview"]["components"][0]["stats"]["files"] += 1

    def tamper_change(document):
        component = document["overview"]["components"][0]
        component["change"] = "added" if component["change"] != "added" else "removed"

    def tamper_layout(document):
        document["overview"]["groups"][0]["column"] += 3

    for mutate in (tamper_stats, tamper_change, tamper_layout):
        errors = overview_errors(mutate)
        assert errors and all(path.startswith("overview.") for path, _ in errors), errors


def test_an_overview_that_names_a_missing_node_fails_with_an_overview_path():
    def missing(document):
        document["overview"]["components"][0]["members"] = ["no-such-node"]

    errors = overview_errors(missing)
    assert any(path == "overview.components[0].members[0]" for path, _ in errors), errors


def test_an_overview_that_is_not_an_object_or_has_unknown_keys_is_rejected():
    assert ("overview", "must be a JSON object") in overview_errors(lambda d: d.update(overview=[]))
    assert ("overview.bogus", "unexpected key") in overview_errors(lambda d: d["overview"].update(bogus=1))
    assert any(path == "overview.components[0].bogus" for path, _ in
               overview_errors(lambda d: d["overview"]["components"][0].update(bogus=1)))


def test_an_unexpected_top_level_key_is_still_rejected_beside_the_overview():
    assert ("surprise", "unexpected key") in overview_errors(lambda d: d.update(surprise=1))


def test_the_size_cap_applies_to_the_whole_document_including_the_overview():
    def pad(document):
        document["overview"]["components"][0]["description"] = "x" * MAX_BYTES

    assert any(path == "$" and "exceeds" in reason for path, reason in overview_errors(pad))


def test_validation_of_the_overview_waits_for_a_valid_detailed_graph():
    def break_both(document):
        document["title"] = ""
        document["overview"]["components"][0]["stats"]["files"] += 1

    errors = overview_errors(break_both)
    assert [path for path, _ in errors] == ["title"]


def test_normalize_strips_unknown_overview_keys_with_a_warning_and_keeps_the_rest():
    document, snap = overview_world()
    document["overview"]["bogus"] = 1
    document["overview"]["components"][0]["bogus"] = 2
    document["overview"]["components"][0]["evidence"][0]["bogus"] = 3
    cleaned, warnings = normalize_v2(document)
    assert "overview.bogus: removed unexpected key" in warnings
    assert "overview.components[0].bogus: removed unexpected key" in warnings
    assert "overview.components[0].evidence[0].bogus: removed unexpected key" in warnings
    assert "bogus" not in cleaned["overview"] and "bogus" not in cleaned["overview"]["components"][0]
    assert cleaned["overview"]["components"][0]["members"] == document["overview"]["components"][0]["members"]
    assert "bogus" in document["overview"], "the input is left alone"
    finalized, _ = finalize_v2(cleaned, HEAD, snap, "deterministic")
    assert validate_v2(finalized, HEAD, snap) is None


def test_normalize_never_raises_on_a_malformed_overview():
    document, _ = overview_world()
    for bad in (None, 5, "text", [], {"components": 7}, {"components": [1, None]}):
        document["overview"] = bad
        cleaned, warnings = normalize_v2(document)
        assert isinstance(cleaned, dict) and "overview" in cleaned


# --- moved rows, routine rows with members, after_return and accounting -------------------------------

from paircli_architecture_v2 import accounting  # noqa: E402


def moved_world():
    """A real draft of 5 pure moves, one edited file and 4 lockfiles, finalized and valid."""
    files = [make_file(f"src/new/m{number}.ts", "renamed", old_path=f"src/old/m{number}.ts", no_hunks=True) for number in range(5)]
    files += [make_file("src/app/service.ts", added=source_lines(3))]
    files += [make_file(f"pkg{number}/package-lock.json", added=[f'"v": {number}']) for number in range(4)]
    snap = make_snapshot(files, head=HEAD)
    draft = deterministic_v2_draft(build_skeleton(snap, HEAD), snap, HEAD)
    document, warnings = finalize_v2(draft, HEAD, snap, "deterministic")
    assert warnings == []
    return document, snap


def family_row(document, family, kind="pattern"):
    return next(node for node in document["nodes"] if node["kind"] == kind and node["pattern"]["family"] == family)


def world_errors(mutate):
    document, snap = moved_world()
    mutate(document)
    with pytest.raises(ArchitectureError) as caught:
        validate_v2(document, HEAD, snap)
    return [(error["path"], error["reason"]) for error in caught.value.errors]


def test_a_moved_row_is_valid_with_no_citation_and_has_computed_stats():
    document, snap = moved_world()
    assert validate_v2(document, HEAD, snap) is None
    row = family_row(document, "moved")
    assert row["evidence"] == [] and row["files"] == sorted(row["pattern"]["members"])
    assert row["label"] == "5 files: moved, not edited" and "no content change" in row["description"]
    assert (row["change"], row["state"]) == ("modified", "changed")
    assert row["stats"] == {"files": 5, "additions": 0, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0}
    assert list(row["pattern"]) == ["family", "count", "members"]


MOVED_FAULTS = [
    ("a member with changed lines", lambda d: family_row(d, "moved")["pattern"]["members"].__setitem__(0, "src/app/service.ts"),
     "pattern.members[0]"),
    ("count mismatch", lambda d: family_row(d, "moved")["pattern"].update(count=4), "pattern.count"),
    ("too few members", lambda d: family_row(d, "moved")["pattern"].update(members=family_row(d, "moved")["pattern"]["members"][:2], count=2),
     "pattern.members"),
    ("signature on a moved row", lambda d: family_row(d, "moved")["pattern"].update(signature=None), "pattern.signature"),
    ("a ghost member", lambda d: family_row(d, "moved")["pattern"]["members"].__setitem__(1, "src/new/ghost.ts"), "pattern.members[1]"),
    ("a sample that is not a member", lambda d: family_row(d, "moved")["files"].__setitem__(0, "src/app/service.ts"), "files[0]"),
    ("more than 12 samples", lambda d: family_row(d, "moved")["files"].extend(f"src/new/x{n}.ts" for n in range(8)), "files"),
    ("stats edited", lambda d: family_row(d, "moved")["stats"].update(additions=1), "stats"),
    ("change edited", lambda d: family_row(d, "moved").update(change="added"), "change"),
]


@pytest.mark.parametrize("name, mutate, expected", MOVED_FAULTS, ids=[fault[0] for fault in MOVED_FAULTS])
def test_validate_rejects_a_bad_moved_row(name, mutate, expected):
    errors = world_errors(mutate)
    assert any(expected in path for path, _ in errors), errors


def test_a_moved_member_must_still_be_a_pure_move_in_the_snapshot():
    document, snap = moved_world()
    victim = next(file for file in snap["pr"]["files"] if file["path"] == "src/new/m0.ts")
    victim["old_path"] = victim["path"]
    assert any("not a pure move" in error["reason"] for error in errors_of(document, snap=snap))
    document, snap = moved_world()
    victim = next(file for file in snap["pr"]["files"] if file["path"] == "src/new/m0.ts")
    victim["status"] = "modified"
    assert any("pattern.members[0]" in error["path"] for error in errors_of(document, snap=snap))
    document, snap = moved_world()
    victim = next(file for file in snap["pr"]["files"] if file["path"] == "src/new/m0.ts")
    victim["hunks"] = [hunk(1, 1, "+")]
    assert any("pattern.members[0]" in error["path"] for error in errors_of(document, snap=snap))


def test_only_a_moved_row_is_exempt_from_citations():
    document, snap = moved_world()
    service = next(node for node in document["nodes"] if node["files"] == ["src/app/service.ts"])
    service["evidence"] = []                                   # an ordinary file row still needs one
    assert any(error["path"].endswith("evidence") for error in errors_of(document, snap=snap))
    document, snap = moved_world()
    moved = family_row(document, "moved")
    moved["evidence"] = [ref("src/app/service.ts", 1, 1)]      # a moved row may carry citations, valid or not
    assert validate_v2(document, HEAD, snap) is None


def test_a_moved_row_is_not_dropped_by_enrich_and_a_member_in_two_rows_is_rejected():
    document, snap = moved_world()
    again, warnings = finalize_v2(document, HEAD, snap, "deterministic")
    assert warnings == [] and again == document

    def share(doc):
        moved = family_row(doc, "moved")
        second = copy.deepcopy(moved)
        second["id"] = "moved-two"
        doc["nodes"].append(second)
    assert any("already a member of" in reason for _, reason in world_errors(share))


def test_a_routine_row_stands_for_all_its_members_and_needs_no_evidence():
    document, snap = moved_world()
    row = family_row(document, "routine", kind="routine")
    assert row["pattern"] == {"family": "routine", "count": 4, "members": sorted(row["pattern"]["members"])}
    assert row["stats"]["files"] == 4 and row["state"] == "routine" and row["evidence"] == []
    assert len(row["files"]) <= 15 and set(row["files"]) <= set(row["pattern"]["members"])
    assert list(row) == ["id", "label", "group", "kind", "description", "files", "evidence", "pattern", "change", "state", "stats"]


ROUTINE_FAULTS = [
    ("a member that is not routine", lambda d: family_row(d, "routine", "routine")["pattern"]["members"].__setitem__(0, "src/app/service.ts"),
     "pattern.members[0]"),
    ("count mismatch", lambda d: family_row(d, "routine", "routine")["pattern"].update(count=9), "pattern.count"),
    ("an empty member list", lambda d: family_row(d, "routine", "routine")["pattern"].update(members=[], count=0), "pattern.members"),
    ("a signature", lambda d: family_row(d, "routine", "routine")["pattern"].update(signature=None), "pattern.signature"),
    ("a wrong family on a routine row", lambda d: family_row(d, "routine", "routine")["pattern"].update(family="tests"),
     "pattern.family"),
    ("routine family on a pattern row", lambda d: family_row(d, "moved")["pattern"].update(family="routine"), "pattern.family"),
    ("stats of the sample only", lambda d: family_row(d, "routine", "routine")["stats"].update(files=1), "stats"),
    ("a sample that is not a member", lambda d: family_row(d, "routine", "routine")["files"].__setitem__(0, "src/app/service.ts"), "files[0]"),
    ("more than 15 samples", lambda d: family_row(d, "routine", "routine")["files"].extend(f"pkg{n}/x.lock" for n in range(20)), "files"),
]


@pytest.mark.parametrize("name, mutate, expected", ROUTINE_FAULTS, ids=[fault[0] for fault in ROUTINE_FAULTS])
def test_validate_rejects_a_bad_routine_row(name, mutate, expected):
    errors = world_errors(mutate)
    assert any(expected in path for path, _ in errors), errors


def test_a_routine_member_in_another_row_is_rejected_and_a_routine_rows_own_files_are_not():
    def share(doc):
        service = next(node for node in doc["nodes"] if node["files"] == ["src/app/service.ts"])
        service["files"] = [family_row(doc, "routine", "routine")["pattern"]["members"][0]]
    errors = world_errors(share)
    assert any("is a member of the pattern row" in reason for _, reason in errors), errors
    document, snap = moved_world()
    assert validate_v2(document, HEAD, snap) is None  # the routine row lists its own members in `files`


def test_a_routine_row_without_a_pattern_is_still_accepted_for_older_documents():
    document, snap = moved_world()
    row = family_row(document, "routine", "routine")
    del row["pattern"]
    row["stats"] = {"files": 4, "additions": 4, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0}
    again, warnings = finalize_v2(document, HEAD, snap, "deterministic")
    assert warnings == [] and again["nodes"][document["nodes"].index(row)]["stats"]["files"] == len(row["files"])


def test_normalize_keeps_pattern_on_routine_rows():
    document, _ = moved_world()
    position = document["nodes"].index(family_row(document, "routine", "routine"))
    cleaned, warnings = normalize_v2(document)
    assert cleaned["nodes"][position]["pattern"]["family"] == "routine" and warnings == []
    document["nodes"][position]["pattern"]["extra"] = 1
    cleaned, warnings = normalize_v2(document)
    assert "extra" not in cleaned["nodes"][position]["pattern"]
    assert f"nodes[{position}].pattern.extra: removed unexpected key" in warnings


def after_return_snapshot():
    lines = [{"kind": "ctx", "text": "  return 1;"}, {"kind": "add", "text": "  log();"}, {"kind": "ctx", "text": "}"}]
    twice = [{"kind": "ctx", "text": "  throw e;"}, {"kind": "del", "text": "  gone();"}, {"kind": "ctx", "text": "}"}]
    files = [changed("src/a.ts", hunks=[{"old_start": 1, "old_lines": 2, "new_start": 1, "new_lines": 3, "lines": lines}]),
             changed("src/b.ts", hunks=[{"old_start": 1, "old_lines": 3, "new_start": 1, "new_lines": 2, "lines": twice}]),
             changed("src/c.ts", hunks=[hunk(1, 1, " + ")])]
    return snapshot(files)


def test_after_return_is_a_computed_stat_per_node_and_summed_over_members():
    snap = after_return_snapshot()
    nodes = [node("a", "g", "file", ["src/a.ts"], [ref("src/a.ts", 2)]), node("ab", "g", "service", ["src/a.ts", "src/b.ts"], [ref("src/a.ts", 2), ref("src/b.ts", 1, 1, "old")]),
             node("c", "g", "file", ["src/c.ts"], [ref("src/c.ts", 2)])]
    document, warnings = finalize_v2({"title": "t", "description": "d", "groups": [{"id": "g", "label": "G"}], "nodes": nodes,
                                      "edges": [], "steps": [], "gaps": []}, HEAD, snap, "llm")
    assert warnings == []
    assert [item["stats"]["after_return"] for item in document["nodes"]] == [1, 2, 0]


def test_after_return_cannot_be_edited_or_dropped():
    snap = after_return_snapshot()
    document, _ = finalize_v2({"title": "t", "description": "d", "groups": [{"id": "g", "label": "G"}],
                               "nodes": [node("a", "g", "file", ["src/a.ts"], [ref("src/a.ts", 2)])], "edges": [], "steps": [],
                               "gaps": []}, HEAD, snap, "llm")
    document["nodes"][0]["stats"]["after_return"] = 0
    assert "nodes[0].stats" in paths_of(document, snap=snap)
    document["nodes"][0]["stats"].pop("after_return")
    assert "nodes[0].stats" in paths_of(document, snap=snap)


def test_accounting_counts_the_files_the_rows_stand_for_without_the_validator():
    document, snap = moved_world()
    assert accounting(document, snap) == (len(snap["pr"]["files"]), len(snap["pr"]["files"]), [])
    broken = copy.deepcopy(document)
    family_row(broken, "moved")["pattern"]["members"].pop()          # no longer valid; accounting does not care
    broken["nodes"] = [node for node in broken["nodes"] if node["kind"] != "routine"]
    covered, total, missing = accounting(broken, snap)
    assert (covered, total) == (len(snap["pr"]["files"]) - 5, len(snap["pr"]["files"]))
    assert missing == sorted(missing) and "src/new/m4.ts" in missing and "pkg0/package-lock.json" in missing
    assert accounting({}, snap) == (0, len(snap["pr"]["files"]), sorted(file["path"] for file in snap["pr"]["files"]))
    assert accounting("junk", {}) == (0, 0, [])
    twice = snapshot(snap["pr"]["files"] + snap["pr"]["files"][:2])
    assert accounting(document, twice)[1] == len(snap["pr"]["files"]), "a repeated path counts once"
