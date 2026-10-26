import copy
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paircli_architecture import MAX_BYTES, ArchitectureError
from paircli_architecture_v2 import SnapshotIndex, finalize_v2, node_facts
from paircli_architecture_v2_draft import deterministic_v2_draft
from paircli_overview import (
    DIGEST_NAME, MAX_DIGEST_BYTES, build_overview_digest, deterministic_overview, drop_invalid_overview_steps,
    enrich_overview, finalize_overview, generate_overview, normalize_overview, overview_prompt, validate_overview,
)
from paircli_skeleton import build_skeleton
from snapshot_factory import HEAD, make_file, make_hunk, make_snapshot, many_files, source_lines

STORE, VIEW = "web/src/store.ts", "web/src/view.ts"
ROUTE, SERVICE, LEGACY = "server/src/route.ts", "server/src/service.ts", "server/src/legacy.ts"
TEST, GUIDE, LOCK = "web/src/store.test.ts", "docs/guide.md", "package-lock.json"
MARKER = "SNAPSHOT_ONLY_MARKER_LINE"
DIAGNOSTICS = ("diagnostics", "architecture-llm")


# --- world: a synthetic PR, its detailed v2 document and a golden overview draft --------------

def world_snapshot():
    return make_snapshot([
        make_file(STORE, "added", added=source_lines(12) + [MARKER], start=1),
        make_file(VIEW, "modified", added=source_lines(4), removed=["old view line"], start=10),
        make_file(ROUTE, "modified", added=source_lines(3), removed=["old a", "old b"], start=10),
        make_file(SERVICE, "added", added=source_lines(10), start=1),
        make_file(LEGACY, "deleted", removed=source_lines(6), start=1),
        make_file(TEST, "added", added=source_lines(5), start=1),
        make_file(GUIDE, "modified", added=["new doc"], removed=["old doc"], start=3),
        make_file(LOCK, "modified", added=["a"], removed=["b"], start=1),
    ], title="Add a store and a service")


def cite(snapshot, path, kind="add", side=None, offset=0):
    """A one-line citation of the `offset`-th line of `kind` (add, del or ctx) of the file."""
    file = next(item for item in snapshot["pr"]["files"] if item["path"] == path)
    found = [line for hunk in file["hunks"] for line in hunk["lines"] if line["kind"] == kind]
    line = found[offset]
    side = side or ("old" if kind == "del" else "new")
    number = line["old_no"] if side == "old" else line["new_no"]
    return {"type": "diff", "path": path, "start": number, "end": number, "side": side}


def node(snapshot, path, group, kind="file", label=None):
    name = path.replace("/", "-").replace(".", "-")
    refs = [] if kind == "routine" else [cite(snapshot, path, "del" if path == LEGACY else "add",
                                              offset=0)]
    return {"id": name, "label": label or path.rsplit("/", 1)[-1], "group": group, "kind": kind,
            "description": f"Changes {path}.", "files": [path], "evidence": refs}


def world_draft(snapshot):
    nodes = [node(snapshot, STORE, "web"), node(snapshot, VIEW, "web"), node(snapshot, ROUTE, "server"),
             node(snapshot, SERVICE, "server"), node(snapshot, LEGACY, "server"), node(snapshot, TEST, "web"),
             node(snapshot, GUIDE, "docs"), node(snapshot, LOCK, "root", kind="routine")]
    edges = [
        {"id": "e-view-store", "source": "web-src-view-ts", "target": "web-src-store-ts", "label": "imports",
         "claim": "view imports store.", "evidence": [cite(snapshot, VIEW, "add", offset=1)]},
        {"id": "e-route-service", "source": "server-src-route-ts", "target": "server-src-service-ts",
         "label": "imports", "claim": "route imports service.", "evidence": [cite(snapshot, ROUTE, "add", offset=0)]},
        {"id": "e-route-store", "source": "server-src-route-ts", "target": "web-src-store-ts", "label": "imports",
         "claim": "route imports store.", "evidence": [cite(snapshot, ROUTE, "add", offset=1)]},
        {"id": "e-service-store", "source": "server-src-service-ts", "target": "web-src-store-ts",
         "label": "imports", "claim": "service imports store.", "evidence": [cite(snapshot, SERVICE, "add", offset=2)]},
    ]
    return {"title": "Add a store", "description": "Adds a store and a service.",
            "groups": [{"id": "web", "label": "Web"}, {"id": "server", "label": "Server"},
                       {"id": "docs", "label": "Docs"}, {"id": "root", "label": "Root"}],
            "nodes": nodes, "edges": edges, "steps": [], "gaps": []}


def build_world():
    snapshot = world_snapshot()
    document, warnings = finalize_v2(world_draft(snapshot), HEAD, snapshot, "deterministic")
    assert warnings == []
    return snapshot, document


def ids(*paths):
    return [path.replace("/", "-").replace(".", "-") for path in paths]


def golden_draft(snapshot):
    """A valid model draft: six components (one a context component), four edges, two steps."""
    return {
        "groups": [{"id": "client", "label": "Client"}, {"id": "server", "label": "Server"}],
        "components": [
            {"id": "store", "label": "Store", "group": "client", "kind": "component",
             "description": "Holds the application state.", "members": ids(STORE)},
            {"id": "view", "label": "View", "group": "client", "kind": "component",
             "description": "Renders state from the store.", "members": ids(VIEW)},
            {"id": "route", "label": "Route", "group": "server", "kind": "component",
             "description": "Handles requests and calls the service.", "members": ids(ROUTE)},
            {"id": "service", "label": "Service", "group": "server", "kind": "component",
             "description": "Does the work for the route.", "members": ids(SERVICE)},
            {"id": "legacy", "label": "Legacy", "group": "server", "kind": "component",
             "description": "The old module that is deleted.", "members": ids(LEGACY)},
            {"id": "db", "label": "Database", "group": "server", "kind": "context",
             "description": "Rows the service writes.", "members": [],
             "evidence": [cite(snapshot, SERVICE, "add", offset=3)]},
        ],
        "edges": [
            {"id": "o-render", "source": "view", "target": "store", "label": "read state",
             "claim": "The view reads state from the store.", "evidence": [cite(snapshot, VIEW, "add", offset=1)]},
            {"id": "o-call", "source": "route", "target": "service", "label": "call service",
             "claim": "The route calls the service.", "evidence": [cite(snapshot, ROUTE, "add", offset=0)]},
            {"id": "o-save", "source": "service", "target": "db", "label": "save rows",
             "claim": "The service writes rows.", "evidence": [cite(snapshot, SERVICE, "add", offset=3)]},
            {"id": "o-fetch", "source": "view", "target": "route", "label": "fetch",
             "claim": "The view fetches from the route.", "evidence": [cite(snapshot, VIEW, "add", offset=2)]},
        ],
        "steps": [
            {"id": "o-s1", "when": "after", "edge": "o-call", "components": ["route", "service"],
             "text": "The route now calls the service.", "evidence": [cite(snapshot, ROUTE, "add", offset=0)]},
            {"id": "o-s0", "when": "before", "components": ["legacy"],
             "text": "A legacy module used to do this.", "evidence": [cite(snapshot, LEGACY, "del", offset=0)]},
        ],
    }


BUILT = build_world()


@pytest.fixture
def world():
    return copy.deepcopy(BUILT)


@pytest.fixture
def snapshot():
    return copy.deepcopy(BUILT[0])


@pytest.fixture
def document():
    return copy.deepcopy(BUILT[1])


@pytest.fixture
def golden(world):
    snapshot, document = world
    overview, warnings = finalize_overview(golden_draft(snapshot), document, HEAD, snapshot, "llm")
    assert warnings == []
    return copy.deepcopy(overview)


def errors_of(overview, document, snapshot):
    try:
        validate_overview(overview, document, HEAD, snapshot)
    except ArchitectureError as error:
        return error.all_errors
    return []


def assert_error(overview, document, snapshot, path, fragment=""):
    found = errors_of(overview, document, snapshot)
    assert any(item["path"] == path and fragment in item["reason"] for item in found), (path, fragment, found)


def component(overview, name):
    return next(item for item in overview["components"] if item["id"] == name)


# --- normalize ---------------------------------------------------------------------------

def test_normalize_strips_unknown_keys_at_every_level_with_warnings(snapshot):
    draft = golden_draft(snapshot)
    draft["extra"] = 1
    draft["groups"][0]["color"] = "red"
    draft["components"][0]["owner"] = "x"
    draft["components"][0]["stats"] = {"files": 1, "bogus": 2}
    draft["components"][0]["evidence"] = [{"type": "diff", "path": STORE, "start": 1, "end": 1, "side": "new", "why": "x"}]
    draft["edges"][0]["weight"] = 3
    draft["steps"][0]["note"] = "x"
    clean, warnings = normalize_overview(draft)
    assert "extra" not in clean and "color" not in clean["groups"][0] and "owner" not in clean["components"][0]
    assert clean["components"][0]["stats"] == {"files": 1}
    assert "why" not in clean["components"][0]["evidence"][0]
    assert "weight" not in clean["edges"][0] and "note" not in clean["steps"][0]
    for path in ("overview.extra", "overview.groups[0].color", "overview.components[0].owner",
                 "overview.components[0].stats.bogus", "overview.components[0].evidence[0].why",
                 "overview.edges[0].weight", "overview.steps[0].note"):
        assert f"{path}: removed unexpected key" in warnings
    assert "extra" in draft  # the input is not modified


def test_normalize_fills_defaults(snapshot):
    clean, warnings = normalize_overview({
        "components": [{"id": "a", "evidence": [{"path": STORE, "start": 1, "end": 1}]}],
        "steps": [{"id": "s"}]})
    assert clean["components"][0]["kind"] == "component" and clean["components"][0]["members"] == []
    assert clean["components"][0]["evidence"][0] == {"path": STORE, "start": 1, "end": 1, "type": "diff", "side": "new"}
    assert clean["steps"][0]["flags"] == [] and warnings == []


@pytest.mark.parametrize("value", [None, [], "text", 7, [{"components": []}], True])
def test_normalize_never_raises_on_non_dict(value):
    clean, warnings = normalize_overview(value)
    assert clean == value and warnings == ["overview: expected a JSON object"]


def test_normalize_tolerates_malformed_containers():
    clean, _ = normalize_overview({"groups": "x", "components": [1, None, {"evidence": 3}], "edges": {"a": 1},
                                   "steps": [[]]})
    assert clean["groups"] == "x" and clean["components"][:2] == [1, None] and clean["steps"] == [[]]


def test_normalize_caps_warnings():
    _, warnings = normalize_overview({f"k{index}": 1 for index in range(300)})
    assert len(warnings) <= 101


# --- enrich ------------------------------------------------------------------------------

def test_enrich_computes_component_facts(world):
    snapshot, document = world
    overview = enrich_overview(normalize_overview(golden_draft(snapshot))[0], document, snapshot, "llm")
    store, legacy, db = (component(overview, name) for name in ("store", "legacy", "db"))
    assert (store["change"], store["state"]) == ("added", "changed")
    assert store["stats"] == {"files": 1, "additions": 13, "deletions": 0, "api_added": 0, "api_removed": 0}
    assert (component(overview, "view")["change"], legacy["change"]) == ("modified", "removed")
    assert legacy["stats"]["deletions"] == 6
    assert (db["change"], db["state"], db["members"]) == ("unchanged", "context", [])
    assert db["stats"] == {key: 0 for key in ("files", "additions", "deletions", "api_added", "api_removed")}
    assert db["evidence"] == [cite(snapshot, SERVICE, "add", offset=3)]
    assert store["evidence"] == document["nodes"][0]["evidence"]
    assert list(store) == ["id", "label", "group", "kind", "description", "members", "evidence", "change", "state", "stats"]
    assert overview["generated_by"] == "llm" and overview["steps"][0]["flags"] == []


def test_enrich_computes_other_for_unclaimed_rows(world):
    snapshot, document = world
    overview = enrich_overview(golden_draft(snapshot), document, snapshot, "llm")
    other = component(overview, "other")
    assert other["label"] == "Other changes" and other["kind"] == "component"
    assert other["members"] == ids(TEST, GUIDE, LOCK)
    assert other["stats"]["files"] == 3 and other["stats"]["additions"] == 5 + 1 + 1
    assert other["change"] == "modified" and other["state"] == "changed"
    assert {ref["path"] for ref in other["evidence"]} == {TEST, GUIDE}
    assert other["group"] == "other" and {"id": "other", "label": "Other changes"}.items() <= \
        next(group for group in overview["groups"] if group["id"] == "other").items()
    assert overview["components"][-1] == other


def test_enrich_has_no_other_when_everything_is_claimed(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    draft["components"].append({"id": "rest", "label": "Rest", "group": "client", "kind": "component",
                                "description": "Tests, docs and config.", "members": ids(TEST, GUIDE)})
    overview = enrich_overview(draft, document, snapshot, "llm")
    assert [item["id"] for item in overview["components"]].count("other") == 1  # the routine lock file is left
    document["nodes"] = [item for item in document["nodes"] if item["kind"] != "routine"]
    overview = enrich_overview(draft, document, snapshot, "llm")
    assert all(item["id"] != "other" for item in overview["components"])
    assert all(group["id"] != "other" for group in overview["groups"])


def test_enrich_overwrites_model_supplied_computed_fields(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    draft["generated_by"] = "deterministic"
    draft["components"][0].update(change="removed", state="routine", stats={"files": 99}, evidence=[cite(snapshot, VIEW)])
    draft["components"][5].update(change="added", state="changed", stats={"files": 4})
    draft["groups"][0].update(path="nowhere", column=7, order=9)
    draft["edges"][0]["change"] = "removed"
    draft["steps"][0]["flags"] = ["risky"]
    overview = enrich_overview(draft, document, snapshot, "llm")
    reference = enrich_overview(golden_draft(snapshot), document, snapshot, "llm")
    assert overview == reference
    assert overview["generated_by"] == "llm"


def test_enrich_member_evidence_is_largest_first_round_robin_and_capped(world):
    snapshot, document = world
    big, small = document["nodes"][0], document["nodes"][1]
    big["evidence"] = [cite(snapshot, STORE, "add", offset=n) for n in range(10)]
    small["evidence"] = [cite(snapshot, VIEW, "add", offset=n) for n in range(4)]
    draft = {"groups": [{"id": "g", "label": "G"}],
             "components": [{"id": "both", "label": "Both", "group": "g", "kind": "component", "description": "x",
                             "members": [small["id"], big["id"]]}], "edges": [], "steps": []}
    refs = enrich_overview(draft, document, snapshot, "llm")["components"][0]["evidence"]
    assert len(refs) == 12
    assert refs[0]["path"] == STORE and refs[1]["path"] == VIEW and refs[2]["path"] == STORE
    assert refs[0]["start"] == big["evidence"][0]["start"] and refs[2]["start"] == big["evidence"][1]["start"]


def test_enrich_uses_the_own_stats_of_pattern_members(world):
    snapshot, document = world
    pattern = {"id": "tests-pattern", "label": "3 files: same change", "group": "web", "kind": "pattern",
               "description": "Same edit.", "files": [TEST], "evidence": [cite(snapshot, TEST)],
               "pattern": {"family": "same-change", "count": 3, "signature": "x", "members": [TEST, GUIDE, LOCK]},
               "change": "modified", "state": "changed",
               "stats": {"files": 3, "additions": 30, "deletions": 9, "api_added": 4, "api_removed": 2}}
    document["nodes"].append(pattern)
    draft = {"groups": [{"id": "g", "label": "G"}],
             "components": [{"id": "pat", "label": "Pattern", "group": "g", "kind": "component", "description": "x",
                             "members": ["tests-pattern", ids(STORE)[0]]}], "edges": [], "steps": []}
    overview = enrich_overview(draft, document, snapshot, "llm")
    stats = component(overview, "pat")["stats"]
    assert stats == {"files": 4, "additions": 43, "deletions": 9, "api_added": 4, "api_removed": 2}
    assert component(overview, "pat")["change"] == "modified"
    assert "tests-pattern" not in component(overview, "other")["members"]


def test_enrich_other_stats_sum_api_fields(world):
    snapshot, document = world
    for item in document["nodes"][:2]:
        item["stats"] = dict(item["stats"], api_added=3, api_removed=1)
    draft = {"groups": [{"id": "g", "label": "G"}], "components": [], "edges": [], "steps": []}
    other = component(enrich_overview(draft, document, snapshot, "llm"), "other")
    assert other["stats"]["api_added"] == 6 and other["stats"]["api_removed"] == 2


def test_enrich_ignores_a_model_component_named_other(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    draft["components"].append({"id": "other", "label": "Mine", "group": "client", "kind": "component",
                                "description": "x", "members": ids(STORE)})
    notes = []
    overview = enrich_overview(draft, document, snapshot, "llm", notes)
    assert any("reserved" in message for message in notes)
    assert component(overview, "other")["label"] == "Other changes"
    assert ids(STORE)[0] in component(overview, "store")["members"]


def test_enrich_drops_unused_groups_and_lays_groups_out(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    draft["groups"].append({"id": "empty", "label": "Empty"})
    overview = enrich_overview(draft, document, snapshot, "llm")
    by_id = {group["id"]: group for group in overview["groups"]}
    assert "empty" not in by_id
    # view (client) fetches from route (server): client is left of server
    assert (by_id["client"]["column"], by_id["server"]["column"]) == (0, 1)
    assert by_id["client"]["path"] == "web/src" and by_id["server"]["path"] == "server/src"
    assert list(by_id["client"]) == ["id", "label", "path", "column", "order"]


def test_enrich_puts_other_in_the_heaviest_group_when_groups_are_full(world):
    snapshot, document = world
    claims = [ids(STORE), ids(VIEW, ROUTE), ids(SERVICE), ids(LEGACY), ids(TEST), ids(GUIDE), [], []]
    draft = {"groups": [{"id": f"g{n}", "label": f"G{n}"} for n in range(8)], "edges": [], "steps": [], "components": [
        {"id": f"c{n}", "label": f"C{n}", "group": f"g{n}", "kind": "context" if not members else "component",
         "description": "x", "members": members, "evidence": [cite(snapshot, SERVICE)] if not members else []}
        for n, members in enumerate(claims)]}
    overview = enrich_overview(draft, document, snapshot, "llm")
    assert len(overview["groups"]) == 8 and "other" not in {group["id"] for group in overview["groups"]}
    assert component(overview, "other")["group"] == "g1"        # the group with two files
    # with a group to spare, `other` gets its own
    draft["components"].pop()
    draft["groups"].pop()
    assert "other" in {group["id"] for group in enrich_overview(draft, document, snapshot, "llm")["groups"]}


def test_enrich_keeps_valid_citations_and_leaves_all_invalid_lists_for_validation(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    bad = {"type": "diff", "path": "nope.ts", "start": 1, "end": 1, "side": "new"}
    draft["edges"][0]["evidence"].append(bad)
    draft["edges"][1]["evidence"] = [bad]
    notes = []
    overview = enrich_overview(draft, document, snapshot, "llm", notes)
    assert overview["edges"][0]["evidence"] == [cite(snapshot, VIEW, "add", offset=1)]
    assert overview["edges"][1]["evidence"] == [bad]
    assert any("overview.edges[0].evidence" in message for message in notes)
    with pytest.raises(ArchitectureError) as caught:
        finalize_overview(draft, document, HEAD, snapshot, "llm")
    assert any(item["path"] == "overview.edges[1].evidence" for item in caught.value.all_errors)


def test_enrich_converts_step_evidence_to_the_side_of_the_step(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    # ROUTE: context line 10 sits on both sides at line 10; the trailing context is old 13 / new 14.
    draft["steps"] = [
        {"id": "o-b", "when": "before", "components": ["route"], "text": "Before.",
         "evidence": [cite(snapshot, ROUTE, "ctx", side="new", offset=0), cite(snapshot, ROUTE, "add", offset=0)]},
        {"id": "o-a", "when": "after", "components": ["route"], "text": "After.",
         "evidence": [cite(snapshot, ROUTE, "ctx", side="old", offset=1), cite(snapshot, ROUTE, "del", offset=0)]}]
    notes = []
    overview = enrich_overview(draft, document, snapshot, "llm", notes)
    before, after = overview["steps"]
    assert before["evidence"] == [{"type": "diff", "path": ROUTE, "start": 10, "end": 10, "side": "old"}]
    assert after["evidence"] == [{"type": "diff", "path": ROUTE, "start": 14, "end": 14, "side": "new"}]
    assert sum("citation dropped" in message for message in notes) == 2
    assert all(message.startswith("overview.steps[") for message in notes if "citation dropped" in message)


MALFORMED = [
    {}, {"components": "x"}, {"components": [1, None, {"id": 5, "members": 3, "evidence": "x"}]},
    {"groups": [3, {"id": None}], "components": [{"id": "a", "group": 3, "kind": "context", "evidence": [1]}]},
    {"edges": [{}, 3, {"evidence": [None], "source": []}], "steps": [[], {"when": "after", "evidence": "x"}]},
    {"steps": [{"when": 7, "evidence": [{"path": 3}]}, {"when": "before", "evidence": [{"type": "diff", "path": STORE,
                                                                                     "start": "a", "end": 2}]}]},
    {"groups": None, "components": None, "edges": None, "steps": None},
    {"components": [{"id": "a", "members": [None, 1, {"x": 1}], "stats": 5}], "groups": {"id": "g"}},
]


@pytest.mark.parametrize("draft", MALFORMED)
def test_enrich_and_finalize_never_crash_on_malformed_drafts(draft, world):
    snapshot, document = world
    enrich_overview(draft, document, snapshot, "llm")
    drop_invalid_overview_steps(enrich_overview(draft, document, snapshot, "llm"), document, HEAD, snapshot)
    with pytest.raises(ArchitectureError):
        finalize_overview(draft, document, HEAD, snapshot, "llm")


def test_finalize_survives_random_damage_to_a_valid_draft(world):
    import random
    snapshot, document = world
    rng = random.Random(7)
    junk = [None, 0, -1, True, "", "x" * 500, [], {}, [None], {"a": 1}, 1.5, "other", float("inf")]

    def damage(value):
        if isinstance(value, dict) and value:
            key = rng.choice(sorted(value))
            if rng.random() < 0.3:
                del value[key]
            else:
                value[key] = rng.choice(junk) if rng.random() < 0.5 else damage(value[key])
        elif isinstance(value, list) and value:
            position = rng.randrange(len(value))
            if rng.random() < 0.3:
                del value[position]
            else:
                value[position] = rng.choice(junk) if rng.random() < 0.5 else damage(value[position])
        else:
            return rng.choice(junk)
        return value

    for _ in range(400):
        draft = golden_draft(snapshot)
        for _round in range(rng.randrange(1, 4)):
            damage(draft)
        try:
            overview, _ = finalize_overview(draft, document, HEAD, snapshot, "llm")
        except ArchitectureError:
            continue
        validate_overview(overview, document, HEAD, snapshot)        # whatever survives is valid


@pytest.mark.parametrize("draft", [None, [], "x", 3])
def test_enrich_returns_non_dict_drafts_and_finalize_reports_them(draft, world):
    snapshot, document = world
    assert enrich_overview(draft, document, snapshot, "llm") == draft
    with pytest.raises(ArchitectureError) as caught:
        finalize_overview(draft, document, HEAD, snapshot, "llm")
    assert caught.value.errors[0]["path"] == "$"


def test_enrich_rejects_an_unknown_generated_by(world):
    snapshot, document = world
    with pytest.raises(ArchitectureError):
        enrich_overview({}, document, snapshot, "human")


def test_enrich_does_not_modify_its_inputs(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    before = (copy.deepcopy(draft), copy.deepcopy(document))
    enrich_overview(draft, document, snapshot, "llm")
    assert (draft, document) == before


# --- validate: the golden overview and each rule failing separately ----------------------

def test_golden_overview_is_valid_and_complete(golden, document, snapshot):
    validate_overview(golden, document, HEAD, snapshot)
    assert list(golden) == ["generated_by", "groups", "components", "edges", "steps"]
    assert len(golden["components"]) == 7 and component(golden, "other")["members"] == ids(TEST, GUIDE, LOCK)
    assert [step["id"] for step in golden["steps"]] == ["o-s1", "o-s0"]
    assert [edge["change"] for edge in golden["edges"]] == ["added", "added", "added", "added"]
    assert len(json.dumps(golden)) < MAX_BYTES


# Each mutation breaks one rule of the golden overview; MUTATIONS lists (mutation, error path, reason fragment).

def m_label_long(o, d):
    o["components"][0]["label"] = "x" * 41


def m_too_many_components(o, d):
    base = o["components"][0]
    for n in range(14):
        o["components"].append(dict(base, id=f"extra{n}", members=[]) | {"kind": "context"})


def m_dup_component(o, d):
    o["components"][1]["id"] = "store"


def m_bad_id(o, d):
    o["components"][0]["id"] = "1 bad"


def m_missing_member(o, d):
    o["components"][0]["members"] = ["no-such-node"]


def m_double_claim(o, d):
    o["components"][1]["members"] = list(o["components"][0]["members"])


def m_routine_member(o, d):
    o["components"][0]["members"] = ids(LOCK)


def m_too_many_members(o, d):
    o["components"][0]["members"] = ids(STORE) * 41


def m_duplicate_member(o, d):
    o["components"][0]["members"] = ids(STORE) * 2


def m_empty_members(o, d):
    o["components"][0]["members"] = []


def m_context_members(o, d):
    component(o, "db")["members"] = ids(TEST)


def m_context_no_evidence(o, d):
    component(o, "db")["evidence"] = []


def m_context_no_edge(o, d):
    o["edges"] = [edge for edge in o["edges"] if "db" not in (edge["source"], edge["target"])]
    o["steps"] = []


def m_other_missing(o, d):
    o["components"] = [item for item in o["components"] if item["id"] != "other"]


def m_other_wrong_members(o, d):
    component(o, "other")["members"] = ids(TEST)


def m_other_unneeded(o, d):
    o["components"].append({"id": "other", "label": "x", "group": "client", "kind": "component", "description": "x",
                            "members": []})
    o["components"].pop(-2)
    o["components"][-1]["id"] = "other"


def m_group_missing(o, d):
    o["components"][0]["group"] = "nowhere"


def m_too_many_groups(o, d):
    o["groups"] += [{"id": f"x{n}", "label": "X", "path": "", "column": 0, "order": 0} for n in range(8)]


def m_group_label(o, d):
    o["groups"][0]["label"] = "g" * 41


def m_group_unused(o, d):
    o["groups"].append({"id": "lonely", "label": "Lonely", "path": "", "column": 0, "order": 5})


def m_edge_label_long(o, d):
    o["edges"][0]["label"] = "x" * 25


def m_edge_label_words(o, d):
    o["edges"][0]["label"] = "a b c d"


def m_claim_long(o, d):
    o["edges"][0]["claim"] = "x" * 301


def m_edge_missing_endpoint(o, d):
    o["edges"][0]["target"] = "ghost"


def m_edge_same_endpoint(o, d):
    o["edges"][0]["target"] = o["edges"][0]["source"]


def m_too_many_edges(o, d):
    o["edges"] = [dict(o["edges"][0], id=f"o-x{n}") for n in range(31)]
    o["steps"] = []


def m_edge_bad_hunk(o, d):
    o["edges"][0]["evidence"][0]["start"] = 900
    o["edges"][0]["evidence"][0]["end"] = 900


def m_edge_no_evidence(o, d):
    o["edges"][0]["evidence"] = []


def m_edge_change(o, d):
    o["edges"][0]["change"] = "removed"


def m_step_before_new_side(o, d):
    o["steps"][1]["evidence"] = [{"type": "diff", "path": ROUTE, "start": 11, "end": 11, "side": "new"}]


def m_step_text_long(o, d):
    o["steps"][0]["text"] = "x" * 401


def m_step_flags(o, d):
    o["steps"][0]["flags"] = ["x"]


def m_step_no_components(o, d):
    o["steps"][0]["components"] = []


def m_step_unknown_component(o, d):
    o["steps"][0]["components"] = ["ghost"]


def m_step_edge_endpoint_outside(o, d):
    o["steps"][0]["components"] = ["route"]


def m_step_after_removed_component(o, d):
    o["steps"][0].update(components=["legacy"], edge=None)


def m_step_before_added_component(o, d):
    o["steps"][1]["components"] = ["store"]


def m_step_after_removed_edge(o, d):
    o["edges"].append({"id": "o-gone", "source": "route", "target": "legacy", "label": "call", "claim": "Called it.",
                       "change": "removed", "evidence": [cite_old_route(o)]})
    o["steps"][0].update(edge="o-gone", components=["route", "legacy"])


def m_too_many_steps(o, d):
    o["steps"] = [dict(o["steps"][0], id=f"o-s{n}") for n in range(11)]


def m_dup_step_id(o, d):
    o["steps"][1]["id"] = "o-s1"


def m_stats(o, d):
    o["components"][0]["stats"]["additions"] += 1


def m_state(o, d):
    o["components"][0]["state"] = "context"


def m_member_evidence(o, d):
    o["components"][0]["evidence"] = []


def m_column(o, d):
    o["groups"][0]["column"] = 5


def m_missing_key(o, d):
    del o["edges"]


def m_extra_key(o, d):
    o["components"][0]["owner"] = "x"
    o["bonus"] = 1


def m_generated_by(o, d):
    o["generated_by"] = "human"


def m_head(o, d):
    d["head_sha"] = "b" * 40


def m_size(o, d):
    d["description"] = "x" * (MAX_BYTES + 10)


def cite_old_route(o):
    return {"type": "diff", "path": ROUTE, "start": 11, "end": 11, "side": "old"}


MUTATIONS = [
    (m_label_long, "overview.components[0].label", "1-40"),
    (m_too_many_components, "overview.components", "1 to 20"),
    (m_dup_component, "overview.components[1].id", "duplicate id"),
    (m_bad_id, "overview.components[0].id", "must start with a letter"),
    (m_missing_member, "overview.components[0].members[0]", "existing detailed node id"),
    (m_double_claim, "overview.components[1].members[0]", "already a member of overview.components[0]"),
    (m_routine_member, "overview.components[0].members[0]", "routine"),
    (m_too_many_members, "overview.components[0].members", "1-40"),
    (m_duplicate_member, "overview.components[0].members[1]", "duplicate member"),
    (m_empty_members, "overview.components[0].members", "1-40"),
    (m_context_members, "overview.components[5].members", "no members"),
    (m_context_no_evidence, "overview.components[5].evidence", "needs at least one valid diff citation"),
    (m_context_no_edge, "overview.components[5]", "endpoint of at least one edge"),
    (m_other_missing, "overview.components", "`other` is missing"),
    (m_other_wrong_members, "overview.components[6].members", "exactly the rows no other component claims"),
    (m_group_missing, "overview.components[0].group", "declared group"),
    (m_too_many_groups, "overview.groups", "1 to 8"),
    (m_group_label, "overview.groups[0].label", "1-40"),
    (m_group_unused, "overview.groups[3]", "no components"),
    (m_edge_label_long, "overview.edges[0].label", "24 characters and 3 words"),
    (m_edge_label_words, "overview.edges[0].label", "24 characters and 3 words"),
    (m_claim_long, "overview.edges[0].claim", "1-300"),
    (m_edge_missing_endpoint, "overview.edges[0].target", "existing component id"),
    (m_edge_same_endpoint, "overview.edges[0]", "must differ"),
    (m_too_many_edges, "overview.edges", "at most 30"),
    (m_edge_bad_hunk, "overview.edges[0].evidence[0].start", "hunk"),
    (m_edge_no_evidence, "overview.edges[0].evidence", "at least one valid diff citation"),
    (m_edge_change, "overview.edges[0].change", "as computed"),
    (m_step_before_new_side, "overview.steps[1].evidence[0].side", "old-side"),
    (m_step_text_long, "overview.steps[0].text", "1-400"),
    (m_step_flags, "overview.steps[0].flags", "empty list"),
    (m_step_no_components, "overview.steps[0].components", "1-16"),
    (m_step_unknown_component, "overview.steps[0].components[0]", "existing component id"),
    (m_step_edge_endpoint_outside, "overview.steps[0].edge", "both endpoints"),
    (m_step_after_removed_component, "overview.steps[0].components[0]", "after step cannot use a component that was removed"),
    (m_step_before_added_component, "overview.steps[1].components[0]", "before step cannot use a component that was added"),
    (m_step_after_removed_edge, "overview.steps[0].edge", "after step cannot use an edge that was removed"),
    (m_too_many_steps, "overview.steps", "at most 10"),
    (m_dup_step_id, "overview.steps[1].id", "duplicate id"),
    (m_stats, "overview.components[0].stats", "as computed"),
    (m_state, "overview.components[0].state", "as computed"),
    (m_member_evidence, "overview.components[0].evidence", "copied from its members"),
    (m_column, "overview.groups[0].column", "as computed"),
    (m_missing_key, "overview.edges", "missing"),
    (m_extra_key, "overview.components[0].owner", "unexpected key"),
    (m_extra_key, "overview.bonus", "unexpected key"),
    (m_generated_by, "overview.generated_by", "must be one of"),
    (m_head, "overview", "current head"),
    (m_size, "$", "exceeds"),
]


@pytest.mark.parametrize("mutation,path,fragment", MUTATIONS, ids=[f"{m.__name__}-{i}" for i, (m, _p, _f) in enumerate(MUTATIONS)])
def test_each_rule_fails_with_its_path(mutation, path, fragment, golden, document, snapshot):
    mutation(golden, document)
    assert_error(golden, document, snapshot, path, fragment)


def test_other_must_not_exist_when_nothing_is_left(golden, document, snapshot):
    document["nodes"] = [item for item in document["nodes"] if item["id"] not in ids(TEST, GUIDE, LOCK)]
    assert_error(golden, document, snapshot, "overview.components[6]", "nothing left over")


def test_an_overview_needs_a_component_of_its_own(document, snapshot):
    overview = enrich_overview({}, document, snapshot, "llm")
    assert_error(overview, document, snapshot, "overview.components", "besides other")


def test_validation_collects_every_error(golden, document, snapshot):
    m_label_long(golden, document)
    m_edge_label_long(golden, document)
    m_step_flags(golden, document)
    m_column(golden, document)
    found = {item["path"] for item in errors_of(golden, document, snapshot)}
    assert {"overview.components[0].label", "overview.edges[0].label", "overview.steps[0].flags",
            "overview.groups[0].column"} <= found


@pytest.mark.parametrize("value", [None, [], "x", 5])
def test_validate_rejects_non_objects(value, document, snapshot):
    assert errors_of(value, document, snapshot) == [{"path": "$", "reason": "must be a JSON object"}]


def test_label_limits_are_exact(golden, document, snapshot):
    golden["components"][0]["label"] = "x" * 40
    golden["edges"][0]["label"] = "a, b c" + "d" * 18          # 24 characters, 3 words
    assert len(golden["edges"][0]["label"]) == 24
    assert errors_of(golden, document, snapshot) == []
    golden["edges"][0]["label"] = "spawn, proxy"
    assert errors_of(golden, document, snapshot) == []


def test_validate_does_not_require_computed_keys_to_be_present(golden, document, snapshot):
    del golden["components"][0]["stats"]
    assert any(item["path"] == "overview.components[0].stats" and item["reason"] == "missing"
               for item in errors_of(golden, document, snapshot))


def test_validate_survives_hostile_overviews(document, snapshot):
    for draft in MALFORMED:
        assert errors_of(draft, document, snapshot)


# --- context components --------------------------------------------------------------------

def test_a_context_component_needs_cited_evidence_and_an_edge(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    draft["components"][5]["evidence"] = []
    draft["edges"] = [edge for edge in draft["edges"] if edge["id"] != "o-save"]
    with pytest.raises(ArchitectureError) as caught:
        finalize_overview(draft, document, HEAD, snapshot, "llm")
    found = {(item["path"], item["reason"]) for item in caught.value.all_errors}
    assert ("overview.components[5].evidence", "a context component needs at least one valid diff citation") in found
    assert ("overview.components[5]", "a context component must be an endpoint of at least one edge") in found


def test_a_context_component_may_cite_old_side_lines(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    draft["components"][5]["evidence"] = [cite(snapshot, LEGACY, "del")]
    overview, _ = finalize_overview(draft, document, HEAD, snapshot, "llm")
    assert component(overview, "db")["evidence"][0]["side"] == "old"


# --- steps ---------------------------------------------------------------------------------

def test_drop_invalid_overview_steps_keeps_the_graph(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    draft["steps"] += [
        {"id": "o-bad1", "when": "before", "components": ["store"], "text": "Store used to exist.",
         "evidence": [cite(snapshot, STORE, "add")]},
        {"id": "o-bad2", "when": "after", "components": ["ghost"], "text": "Ghost.",
         "evidence": [cite(snapshot, STORE, "add")]},
        {"id": "o-s1", "when": "after", "components": ["store"], "text": "Duplicate id.",
         "evidence": [cite(snapshot, STORE, "add")]}]
    enriched = enrich_overview(draft, document, snapshot, "llm")
    result, dropped = drop_invalid_overview_steps(enriched, document, HEAD, snapshot)
    assert [step["id"] for step in result["steps"]] == ["o-s1", "o-s0"]
    assert [entry["id"] for entry in dropped] == ["o-bad1", "o-bad2", "o-s1"]
    assert dropped[0]["errors"][0]["path"].startswith("overview.steps[2]")
    assert {key: result[key] for key in ("groups", "components", "edges")} == \
        {key: enriched[key] for key in ("groups", "components", "edges")}
    validate_overview(result, document, HEAD, snapshot)


def test_finalize_drops_bad_steps_with_warnings_but_raises_on_a_bad_graph(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    draft["steps"].append({"id": "o-x", "when": "after", "components": ["legacy"], "text": "Legacy is used.",
                           "evidence": [cite(snapshot, ROUTE, "add")]})
    overview, warnings = finalize_overview(draft, document, HEAD, snapshot, "llm")
    assert [step["id"] for step in overview["steps"]] == ["o-s1", "o-s0"]
    assert any("o-x dropped" in message and "removed" in message for message in warnings)
    draft["components"][0]["label"] = "x" * 50
    with pytest.raises(ArchitectureError) as caught:
        finalize_overview(draft, document, HEAD, snapshot, "llm")
    assert caught.value.errors[0]["path"] == "overview.components[0].label"


def test_a_step_with_no_citation_left_after_conversion_is_dropped(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    draft["steps"][1]["evidence"] = [cite(snapshot, ROUTE, "add", offset=0)]   # added line cannot support "before"
    overview, warnings = finalize_overview(draft, document, HEAD, snapshot, "llm")
    assert [step["id"] for step in overview["steps"]] == ["o-s1"]
    assert any("citation dropped" in message for message in warnings) and any("o-s0 dropped" in message for message in warnings)


def test_the_edge_of_a_step_must_be_inside_its_components(world):
    snapshot, document = world
    draft = golden_draft(snapshot)
    draft["steps"][0]["components"] = ["route"]
    overview, warnings = finalize_overview(draft, document, HEAD, snapshot, "llm")
    assert [step["id"] for step in overview["steps"]] == ["o-s0"]
    assert any("both endpoints" in message for message in warnings)


# --- deterministic overview ----------------------------------------------------------------

def deterministic(document, snapshot):
    draft = deterministic_overview(document, snapshot)
    overview, warnings = finalize_overview(draft, document, HEAD, snapshot, "deterministic")
    assert warnings == [] and overview["generated_by"] == "deterministic" and overview["steps"] == []
    assert 1 <= len(overview["components"]) <= 20 and 1 <= len(overview["groups"]) <= 8
    assert len(overview["edges"]) <= 30
    claimed = [name for item in overview["components"] for name in item["members"]]
    assert len(claimed) == len(set(claimed))
    return overview


def test_deterministic_overview_of_the_world(world):
    snapshot, document = world
    overview = deterministic(document, snapshot)
    by_id = {item["id"]: item for item in overview["components"]}
    assert set(by_id) == {"c-web", "c-server", "c-docs", "other"}
    assert by_id["c-web"]["label"] == "Web" and by_id["c-web"]["members"] == ids(STORE, VIEW, TEST)
    assert by_id["other"]["members"] == ids(LOCK)           # the routine row
    (edge,) = overview["edges"]
    assert (edge["source"], edge["target"], edge["label"]) == ("c-server", "c-web", "imports")
    assert edge["claim"] == "2 imports between these areas" and len(edge["evidence"]) == 2
    assert {ref["path"] for ref in edge["evidence"]} <= {ROUTE, SERVICE}
    assert edge["id"].startswith("o-")
    groups = {group["id"] for group in overview["groups"]}
    assert all(item["group"] in groups for item in overview["components"])


def test_deterministic_overview_does_not_reuse_the_name_of_the_computed_component(world):
    snapshot, document = world
    document["groups"][2]["label"] = "Other changes"
    overview = deterministic(document, snapshot)
    labels = [item["label"] for item in overview["components"]]
    assert labels.count("Other changes") == 1 and "Smaller areas" in labels


def test_deterministic_overview_is_deterministic(world):
    snapshot, document = world
    first = deterministic_overview(document, snapshot)
    assert deterministic_overview(copy.deepcopy(document), copy.deepcopy(snapshot)) == first
    assert finalize_overview(first, document, HEAD, snapshot, "deterministic") == \
        finalize_overview(copy.deepcopy(first), document, HEAD, snapshot, "deterministic")


def test_deterministic_overview_labels_unrelated_edges_as_uses(world):
    snapshot, document = world
    for edge in document["edges"]:
        edge["label"] = "calls"
    (edge,) = deterministic(document, snapshot)["edges"]
    assert (edge["label"], edge["claim"]) == ("uses", "2 links between these areas")


def test_deterministic_overview_skips_edges_it_cannot_cite(world):
    snapshot, document = world
    for edge in document["edges"]:
        edge["evidence"] = []
    assert deterministic(document, snapshot)["edges"] == []


def synthetic(count, directories=None):
    snapshot = make_snapshot(many_files(count, directories))
    draft = deterministic_v2_draft(build_skeleton(snapshot, HEAD), snapshot, HEAD)
    assert draft is not None
    document, _ = finalize_v2(draft, HEAD, snapshot, "deterministic")
    return snapshot, document


@pytest.mark.parametrize("count", [1, 3, 12, 253, 1000])
def test_deterministic_overview_is_valid_for_synthetic_prs(count):
    snapshot, document = synthetic(count)
    overview = deterministic(document, snapshot)
    # every detailed row is accounted for exactly once
    rows = [name for item in overview["components"] for name in item["members"]]
    assert sorted(rows) == sorted(item["id"] for item in document["nodes"] if item["files"] or item["kind"] == "pattern")


def test_deterministic_overview_handles_a_pattern_row_document(world):
    snapshot, document = world
    document["nodes"].append({
        "id": "tests-pattern", "label": "3 files: same change", "group": "web", "kind": "pattern",
        "description": "Same edit.", "files": [TEST], "evidence": [cite(snapshot, TEST)],
        "pattern": {"family": "same-change", "count": 3, "signature": "x", "members": [TEST, GUIDE, LOCK]},
        "change": "modified", "state": "changed",
        "stats": {"files": 3, "additions": 30, "deletions": 9, "api_added": 4, "api_removed": 2}})
    overview = deterministic(document, snapshot)
    web = component(overview, "c-web")
    assert "tests-pattern" in web["members"] and web["stats"]["files"] == 1 + 1 + 1 + 3
    assert web["stats"]["api_added"] == 4


def hand_document(snapshot, groups: dict[str, list[str]]):
    nodes, edges = [], []
    for group, paths in groups.items():
        for path in paths:
            nodes.append({"id": "n-" + path.replace("/", "-").replace(".", "-"), "label": path.rsplit("/", 1)[-1],
                          "group": group, "kind": "file", "description": f"Changes {path}.", "files": [path],
                          "evidence": [cite(snapshot, path, "add")]})
    document = {"schema_version": 2, "generated_by": "deterministic", "head_sha": HEAD, "title": "t",
                "description": "d", "groups": [{"id": group, "label": group.title(), "path": ""} for group in groups],
                "nodes": nodes, "edges": edges, "steps": [], "gaps": []}
    for item in nodes:
        item.update(node_facts(item, SnapshotIndex(snapshot)))
    return document


def test_a_group_with_more_than_40_rows_is_split_by_directory():
    paths = [f"app/{'alpha' if n < 50 else 'beta'}/f{n}.ts" for n in range(90)]
    snapshot = make_snapshot([make_file(path, added=source_lines(2), removed=["x"]) for path in paths])
    document = hand_document(snapshot, {"app": paths})
    overview = deterministic(document, snapshot)
    sizes = sorted(len(item["members"]) for item in overview["components"])
    assert sum(sizes) == 90 and max(sizes) <= 40 and len(sizes) >= 3
    labels = [item["label"] for item in overview["components"]]
    assert all(label.startswith("App / ") for label in labels)


def test_components_beyond_the_cap_are_merged_into_other():
    paths = [f"area{n:02d}/src/file.ts" for n in range(25)]
    snapshot = make_snapshot([make_file(path, added=source_lines(2 + n), removed=["x"]) for n, path in enumerate(paths)])
    document = hand_document(snapshot, {f"g{n:02d}": [path] for n, path in enumerate(paths)})
    overview = deterministic(document, snapshot)
    assert len(overview["components"]) == 20
    other = component(overview, "other")
    assert len(other["members"]) == 6
    kept = {item["members"][0] for item in overview["components"] if item["id"] != "other"}
    heaviest = {f"n-area{n:02d}-src-file-ts" for n in range(6, 25)}
    assert kept == heaviest                                     # the 19 heaviest groups stay
    assert len(overview["groups"]) <= 8


def test_exactly_20_groups_without_leftovers_need_no_other():
    paths = [f"area{n:02d}/src/file.ts" for n in range(20)]
    snapshot = make_snapshot([make_file(path, added=source_lines(2), removed=["x"]) for path in paths])
    overview = deterministic(hand_document(snapshot, {f"g{n:02d}": [path] for n, path in enumerate(paths)}), snapshot)
    assert len(overview["components"]) == 20 and all(item["id"] != "other" for item in overview["components"])


def test_deterministic_overview_never_raises_on_odd_documents(snapshot):
    for odd in ({}, {"nodes": "x"}, {"nodes": [1, None, {"id": "a"}], "groups": 5, "edges": [3]}):
        draft = deterministic_overview(odd, snapshot)
        assert draft["steps"] == []
    assert deterministic_overview({"nodes": []}, snapshot)["components"] == []


# --- LLM orchestration ---------------------------------------------------------------------

class Stub:
    """A thread-safe stand-in for the model: `answers` is a list of texts, exceptions or callables(prompt)."""

    def __init__(self, answers=None, default=None):
        self.answers = list(answers or [])
        self.default = default
        self.lock = threading.Lock()
        self.calls: list[dict] = []

    def __call__(self, prompt, *, timeout=None, model=None):
        with self.lock:
            self.calls.append({"prompt": prompt, "timeout": timeout, "model": model})
            answer = self.answers.pop(0) if self.answers else self.default
        if callable(answer):
            answer = answer(prompt)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Log:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, message):
        self.lines.append(message)


@pytest.fixture
def artifact(tmp_path, snapshot):
    (tmp_path / "snapshot.json").write_text(json.dumps(snapshot))
    return tmp_path


def run(artifact, world, stub, **kwargs):
    snapshot, document = world
    log = kwargs.pop("log", Log())
    return generate_overview(artifact, snapshot, HEAD, document, stub, log, **kwargs), log


def diagnostics_dir(artifact):
    return artifact.joinpath(*DIAGNOSTICS)


def test_generate_valid_first_try(artifact, world):
    stub = Stub([json.dumps(golden_draft(world[0]))])
    result, _ = run(artifact, world, stub, model="some-model", call_timeout=77)
    assert result["stats"] == {"source": "llm", "retried": False, "steps_dropped": 0, "llm_calls": 1}
    assert result["overview"]["generated_by"] == "llm" and len(result["overview"]["components"]) == 7
    assert result["warnings"] == []
    validate_overview(result["overview"], world[1], HEAD, world[0])
    assert stub.calls[0]["model"] == "some-model" and stub.calls[0]["timeout"] == 77
    folder = diagnostics_dir(artifact)
    assert (folder / DIGEST_NAME).is_file() and (folder / "overview-raw.txt").is_file()
    assert not (folder / "overview-errors.json").exists()


def test_generate_tolerates_fences_and_prose(artifact, world):
    text = "Here is the map:\n```json\n" + json.dumps(golden_draft(world[0])) + "\n```\nHope it helps."
    result, _ = run(artifact, world, Stub([text]))
    assert result["stats"]["source"] == "llm"


def test_generate_overwrites_model_computed_fields(artifact, world):
    draft = golden_draft(world[0])
    draft["components"][0]["change"] = "removed"
    draft["generated_by"] = "deterministic"
    result, _ = run(artifact, world, Stub([json.dumps(draft)]))
    assert result["overview"]["generated_by"] == "llm" and component(result["overview"], "store")["change"] == "added"


def test_generate_retries_once_with_the_structured_errors(artifact, world):
    bad = golden_draft(world[0])
    bad["components"][0]["members"] = ["no-such-node"]
    bad["components"][1]["label"] = "x" * 60
    stub = Stub([json.dumps(bad), json.dumps(golden_draft(world[0]))])
    result, _ = run(artifact, world, stub)
    assert result["stats"] == {"source": "llm", "retried": True, "steps_dropped": 0, "llm_calls": 2}
    first, second = (call["prompt"] for call in stub.calls)
    assert "rejected with these errors" not in first
    assert "overview.components[0].members[0]: must be an existing detailed node id" in second
    assert "overview.components[1].label: must be 1-40 characters" in second
    lines = [line for line in second.splitlines() if line.startswith("overview.")]
    assert 1 <= len(lines) <= 20 and all(len(line) <= 200 for line in lines)
    folder = diagnostics_dir(artifact)
    assert json.loads((folder / "overview-errors.json").read_text())[0]["path"] == "overview.components[0].members[0]"
    assert (folder / "overview-raw-1.txt").is_file() and (folder / "overview-errors-1.json").is_file()


def test_generate_retry_lines_are_capped(artifact, world):
    bad = golden_draft(world[0])
    bad["components"] = [dict(bad["components"][0], id=f"c{n}", members=[f"ghost-{'x' * 300}-{n}", "ghost2"]) for n in range(15)]
    stub = Stub([json.dumps(bad), json.dumps(golden_draft(world[0]))])
    run(artifact, world, stub)
    lines = [line for line in stub.calls[1]["prompt"].splitlines() if line.startswith("overview.")]
    assert len(lines) == 20 and all(len(line) <= 200 for line in lines)


def test_generate_two_invalid_answers_fall_back_to_the_deterministic_overview(artifact, world):
    bad = json.dumps({"components": []})
    stub = Stub([bad, bad])
    result, log = run(artifact, world, stub)
    assert result["stats"] == {"source": "deterministic", "retried": True, "steps_dropped": 0, "llm_calls": 2,
                               "reason": result["stats"]["reason"]}
    assert result["overview"]["generated_by"] == "deterministic" and result["overview"]["steps"] == []
    assert len(stub.calls) == 2 and any("rejected twice" in line for line in log.lines)


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("claude", 5), TimeoutError("slow")])
def test_generate_timeout_falls_back_without_retry(artifact, world, failure):
    stub = Stub([failure, json.dumps(golden_draft(world[0]))])
    result, log = run(artifact, world, stub)
    assert result["stats"]["source"] == "deterministic" and result["stats"]["retried"] is False
    assert len(stub.calls) == 1 and any("timed out" in line for line in log.lines)
    assert result["overview"]["generated_by"] == "deterministic"


def test_generate_call_error_falls_back_without_retry(artifact, world):
    stub = Stub([RuntimeError("boom")])
    result, _ = run(artifact, world, stub)
    assert result["stats"]["source"] == "deterministic" and len(stub.calls) == 1


@pytest.mark.parametrize("answer", ["no json here at all", "{not json", "[1, 2]", "", None, 5])
def test_generate_garbage_falls_back(artifact, world, answer):
    stub = Stub(default=answer)
    result, _ = run(artifact, world, stub)
    assert result["stats"]["source"] == "deterministic" and result["overview"] is not None
    assert len(stub.calls) <= 2


def test_generate_null_falls_back_without_retry(artifact, world):
    stub = Stub(["null"])
    result, _ = run(artifact, world, stub)
    assert result["stats"]["source"] == "deterministic" and len(stub.calls) == 1


def test_generate_oversized_output_is_rejected(artifact, world):
    stub = Stub(default=" " * (300 * 1024) + json.dumps(golden_draft(world[0])))
    result, _ = run(artifact, world, stub)
    assert result["stats"]["source"] == "deterministic"


def test_generate_counts_dropped_steps(artifact, world):
    draft = golden_draft(world[0])
    draft["steps"].append({"id": "o-bad", "when": "after", "components": ["legacy"], "text": "Legacy.",
                           "evidence": [cite(world[0], ROUTE, "add")]})
    result, _ = run(artifact, world, Stub([json.dumps(draft)]))
    assert result["stats"]["steps_dropped"] == 1 and any("o-bad dropped" in item for item in result["warnings"])


def test_generate_respects_the_deadline(artifact, world):
    stub = Stub([json.dumps(golden_draft(world[0]))])
    result, _ = run(artifact, world, stub, deadline_seconds=1)
    assert stub.calls == [] and result["stats"]["source"] == "deterministic"


def test_generate_budget_shrinks_the_retry_timeout(artifact, world):
    now = [0.0]

    def clock():
        return now[0]

    def slow_invalid(prompt):
        now[0] += 80
        return json.dumps({"components": []})

    stub = Stub([slow_invalid, json.dumps(golden_draft(world[0]))])
    result, _ = run(artifact, world, stub, call_timeout=100, deadline_seconds=100, clock=clock)
    assert [call["timeout"] for call in stub.calls] == [100, 20] and result["stats"]["source"] == "llm"
    now[0] = 0.0
    stub = Stub([slow_invalid], default=json.dumps({"components": []}))
    result, _ = run(artifact, world, stub, call_timeout=100, deadline_seconds=81, clock=clock)
    assert len(stub.calls) == 1 and result["stats"]["source"] == "deterministic"


def test_generate_prompt_is_small_and_never_inlines_the_snapshot(artifact, world):
    stub = Stub([json.dumps({"components": []})] * 2)
    run(artifact, world, stub)
    for call in stub.calls:
        prompt = call["prompt"]
        assert len(prompt.encode()) < 12 * 1024
        assert MARKER not in prompt and "Add a store and a service" not in prompt
        assert str(diagnostics_dir(artifact) / DIGEST_NAME) in prompt and str(artifact / "snapshot.json") in prompt
        assert STORE not in prompt
    for rule in ("8 to 20", "context", "at most 8", "RUNTIME", "4 to 8", "OLD-side", "never on the PR title", "digest"):
        assert rule in stub.calls[0]["prompt"].replace("PR title or description", "PR title"), rule


def test_prompt_stays_small_with_a_long_error_list(tmp_path):
    errors = [{"path": "overview.components[%d].members[0]" % n, "reason": "r" * 500} for n in range(50)]
    assert len(overview_prompt(tmp_path, tmp_path / "d.json", errors).encode()) < 12 * 1024


def test_digest_content(artifact, world):
    run(artifact, world, Stub([json.dumps(golden_draft(world[0]))]))
    digest = json.loads((diagnostics_dir(artifact) / DIGEST_NAME).read_text())
    assert digest["pr_title_hint"] == "Add a store and a service"
    assert {item["id"] for item in digest["nodes"]} == {item["id"] for item in world[1]["nodes"]}
    lock = next(item for item in digest["nodes"] if item["kind"] == "routine")
    assert lock["claimable"] is False
    store = next(item for item in digest["nodes"] if item["id"] == ids(STORE)[0])
    assert store["change"] == "added" and store["stats"]["additions"] == 13 and store["evidence"]
    assert [group["id"] for group in digest["groups"]] == ["web", "server", "docs", "root"]
    assert all(group["summary"] for group in digest["groups"])
    assert {edge["id"] for edge in digest["edges"]} == {edge["id"] for edge in world[1]["edges"]}
    assert digest["steps"] == []
    assert {item["id"] for item in digest["baseline"]["components"]} == {"c-web", "c-server", "c-docs"}
    assert MARKER not in json.dumps(digest)


def test_digest_lists_pattern_member_counts(world):
    snapshot, document = world
    document = copy.deepcopy(document)
    document["nodes"].append({"id": "pat", "label": "3 files", "group": "web", "kind": "pattern", "description": "d",
                              "files": [TEST], "evidence": [], "pattern": {"family": "tests", "count": 3,
                                                                           "members": [TEST, GUIDE, LOCK]},
                              "stats": {"files": 3, "additions": 3, "deletions": 0}})
    digest = build_overview_digest(document, snapshot)
    assert next(item for item in digest["nodes"] if item["id"] == "pat")["pattern"] == {"family": "tests", "members": 3}


def test_digest_is_capped_for_huge_documents(artifact, world):
    snapshot, document = world
    document = copy.deepcopy(document)
    template = document["nodes"][0]
    document["nodes"] = [dict(template, id=f"node-{n}", label="L" * 80, description="d" * 1200, evidence=[template["evidence"][0]] * 12)
                         for n in range(400)]
    document["steps"] = [{"id": f"s{n}", "when": "after", "text": "t" * 400} for n in range(24)]
    document["edges"] = []
    digest = build_overview_digest(document, snapshot, {"components": [{"id": f"c{n}", "label": "x" * 40, "members": [f"node-{n}"] * 40}
                                                                       for n in range(20)], "edges": []})
    assert len(json.dumps(digest, separators=(",", ":"))) <= MAX_DIGEST_BYTES and digest["truncated"] is True
    result, _ = run(artifact, (snapshot, document), Stub())
    written = (diagnostics_dir(artifact) / DIGEST_NAME).stat().st_size
    assert written <= MAX_DIGEST_BYTES and result["stats"]["source"] in ("deterministic", "none")


def test_generate_refuses_a_symlinked_diagnostics_folder(tmp_path, world, snapshot):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "diagnostics").symlink_to(elsewhere)
    stub = Stub([json.dumps(golden_draft(world[0]))])
    result, log = run(artifact, world, stub)
    assert stub.calls == [] and result["stats"]["source"] == "deterministic"
    assert list(elsewhere.rglob("*")) == [] and any("not written" in line for line in log.lines)


def test_generate_refuses_a_symlinked_llm_folder_and_symlinked_files(tmp_path, world):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    artifact = tmp_path / "artifact"
    (artifact / "diagnostics").mkdir(parents=True)
    (artifact / "diagnostics" / "architecture-llm").symlink_to(elsewhere)
    stub = Stub()
    result, _ = run(artifact, world, stub)
    assert stub.calls == [] and result["stats"]["source"] == "deterministic" and list(elsewhere.iterdir()) == []

    folder = tmp_path / "artifact2" / "diagnostics" / "architecture-llm"
    folder.mkdir(parents=True)
    target = tmp_path / "victim.txt"
    target.write_text("keep")
    (folder / "overview-raw.txt").symlink_to(target)
    result, _ = run(folder.parents[1], world, Stub([json.dumps(golden_draft(world[0]))]))
    assert target.read_text() == "keep" and result["stats"]["source"] == "llm"


def test_generate_clears_stale_diagnostics_first(artifact, world):
    folder = diagnostics_dir(artifact)
    folder.mkdir(parents=True)
    (folder / "overview-errors.json").write_text("[]")
    (folder / "overview-raw-1.txt").write_text("old")
    (folder / "group-x-input.json").write_text("keep")
    run(artifact, world, Stub([TimeoutError()]))
    assert not (folder / "overview-errors.json").exists() and not (folder / "overview-raw-1.txt").exists()
    assert (folder / "group-x-input.json").read_text() == "keep"


def test_generate_never_raises(tmp_path, world):
    snapshot, document = world
    results = [
        generate_overview(tmp_path, snapshot, HEAD, document, Stub([json.dumps(golden_draft(snapshot))]), object()),
        generate_overview(tmp_path, snapshot, HEAD, document, "not callable", Log()),
        generate_overview(str(tmp_path), None, HEAD, {}, Stub(default="x"), Log()),
        generate_overview(None, snapshot, HEAD, document, Stub(default="x"), Log()),
        generate_overview(tmp_path, snapshot, HEAD, document, Stub(default="x"), Log(), call_timeout="soon",
                          deadline_seconds=float("nan")),
        generate_overview(tmp_path / "missing" / "deeper", snapshot, HEAD, document, Stub(default="x"), Log()),
    ]
    assert results[0]["stats"]["source"] == "llm"
    assert results[1]["stats"]["source"] == "deterministic"
    assert results[2]["overview"] is None and results[2]["stats"]["source"] == "none"
    assert results[3]["stats"]["source"] == "deterministic" or results[3]["overview"] is None
    assert results[4]["stats"]["source"] == "deterministic"
    for result in results:
        assert set(result) == {"overview", "warnings", "stats"}


def test_generate_is_thread_safe(tmp_path, world):
    snapshot, document = world
    stub = Stub(default=json.dumps(golden_draft(snapshot)))
    results, errors = [], []

    def work(number):
        folder = tmp_path / f"run{number}"
        folder.mkdir()
        try:
            results.append(generate_overview(folder, snapshot, HEAD, document, stub, Log()))
        except Exception as error:  # pragma: no cover
            errors.append(error)

    threads = [threading.Thread(target=work, args=(number,)) for number in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and len(stub.calls) == 6
    assert all(result["stats"]["source"] == "llm" for result in results)
    assert all(result["overview"] == results[0]["overview"] for result in results)


def test_generate_is_deterministic_for_a_fixed_answer(tmp_path, world):
    snapshot, document = world
    answers = [generate_overview(tmp_path / name, snapshot, HEAD, document,
                                 Stub([json.dumps(golden_draft(snapshot))]), Log())
               for name in ("a", "b")] if all((tmp_path / name).mkdir() is None for name in ("a", "b")) else []
    assert answers[0]["overview"] == answers[1]["overview"]
