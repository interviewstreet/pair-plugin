import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paircli_architecture import MAX_BYTES, IDENTIFIER
from paircli_architecture_v2 import finalize_v2, validate_v2
from paircli_architecture_v2_draft import (
    deterministic_v2_draft, remove_stale_architecture_v2, write_architecture_v2, write_no_architecture_v2,
)
from paircli_archive import create_pr_archive
from paircli_skeleton import build_skeleton
from snapshot_factory import HEAD, make_file, make_hunk, make_snapshot, many_files, source_lines

ID = re.compile(IDENTIFIER)


def build(files, title="Synthetic change"):
    snapshot = make_snapshot(files, title=title)
    skeleton = build_skeleton(snapshot, HEAD)
    return snapshot, skeleton, deterministic_v2_draft(skeleton, snapshot, HEAD)


def finalize(files):
    """(snapshot, draft, document): the draft must finalize with no warnings and lose nothing."""
    snapshot, _, draft = build(files)
    assert draft is not None
    document, warnings = finalize_v2(draft, HEAD, snapshot, "deterministic")
    assert warnings == []
    assert [node["id"] for node in document["nodes"]] == [node["id"] for node in draft["nodes"]]
    assert [edge["id"] for edge in document["edges"]] == [edge["id"] for edge in draft["edges"]]
    assert [group["id"] for group in document["groups"]] == [group["id"] for group in draft["groups"]]
    assert document["generated_by"] == "deterministic" and document["steps"] == []
    return snapshot, draft, document


def by_file(document):
    return {node["files"][0]: node for node in document["nodes"] if node["kind"] == "file"}


# --- validity on synthetic snapshots ---------------------------------------------------------

def test_a_single_file_pr_gives_a_valid_one_row_draft():
    _, draft, document = finalize([make_file("src/app.ts", added=source_lines(4), removed=["old"])])
    assert [group["id"] for group in draft["groups"]] == [group["id"] for group in document["groups"]]
    assert len(draft["nodes"]) == 1 and draft["edges"] == [] and draft["steps"] == []
    assert document["nodes"][0]["stats"] == {"additions": 4, "deletions": 1, "files": 1}
    assert draft["gaps"][-1].startswith("Generated deterministically")


@pytest.mark.parametrize("count", [12, 34, 253, 1000])
def test_synthetic_prs_of_every_size_finalize_with_nothing_dropped(count):
    snapshot, _, document = finalize(many_files(count))
    validate_v2(document, HEAD, snapshot)
    assert len(json.dumps(document, ensure_ascii=False).encode()) < MAX_BYTES


def test_draft_shape_keeps_to_the_model_writable_fields():
    _, _, draft = build(many_files(12))
    assert set(draft) == {"title", "description", "groups", "nodes", "edges", "steps", "gaps"}
    assert all(set(group) == {"id", "label"} for group in draft["groups"])
    node_keys = {"id", "label", "group", "kind", "description", "files", "evidence"}
    assert all(set(node) == node_keys for node in draft["nodes"])
    assert all(set(edge) == {"id", "source", "target", "label", "claim", "evidence"} for edge in draft["edges"])
    assert all(set(ref) == {"type", "path", "start", "end", "side"} for node in draft["nodes"] for ref in node["evidence"])
    assert draft["title"] == "Synthetic change" and re.fullmatch(r"Changes 12 files in \d+ areas? \(\+72/-12\)\.", draft["description"])


def test_a_long_title_is_capped():
    _, _, draft = build([make_file("a.ts", added=["x"])], title="T" * 500)
    assert len(draft["title"]) == 160


def test_descriptions_say_what_happened_to_each_file():
    files = [make_file("src/m.ts", added=["a", "b"], removed=["c"]), make_file("src/n.ts", status="added", added=["a"]),
             make_file("src/o.ts", status="deleted", removed=["a", "b", "c"]),
             make_file("src/p.ts", status="renamed", old_path="src/q.ts", added=["z"], removed=["y"])]
    _, _, document = finalize(files)
    nodes = by_file(document)
    assert nodes["src/m.ts"]["description"] == "Modified, +2/-1 lines."
    assert nodes["src/n.ts"]["description"] == "Added, +1/-0 lines."
    assert nodes["src/o.ts"]["description"] == "Deleted, +0/-3 lines."
    assert nodes["src/p.ts"]["description"] == "Renamed from src/q.ts, +1/-1 lines."
    assert {name: node["label"] for name, node in nodes.items()}["src/m.ts"] == "m.ts"
    assert all(node["kind"] == "file" and node["files"] == [name] for name, node in nodes.items())


# --- evidence --------------------------------------------------------------------------------

def test_file_evidence_uses_the_biggest_hunks_and_the_changed_lines():
    hunks = [make_hunk(1, added=["a"]), make_hunk(50, added=source_lines(30)), make_hunk(200, added=source_lines(10)),
             make_hunk(400, added=source_lines(5)), make_hunk(600, added=source_lines(2))]
    _, draft, document = finalize([make_file("src/big.ts", hunks=hunks)])
    refs = draft["nodes"][0]["evidence"]
    assert len(refs) == 3 and all(ref["side"] == "new" for ref in refs)
    # context lines around each hunk are not cited: 30, 10 and 5 added lines
    assert [(ref["start"], ref["end"]) for ref in refs] == [(51, 80), (201, 210), (401, 405)]
    assert document["nodes"][0]["stats"]["additions"] == 1 + 30 + 10 + 5 + 2


def test_deleted_files_are_cited_on_the_old_side():
    snapshot, draft, document = finalize([make_file("src/gone.ts", status="deleted", removed=source_lines(6)),
                                          make_file("src/kept.ts", added=["x"])])
    gone = by_file(document)["src/gone.ts"]
    assert gone["change"] == "removed" and gone["evidence"] == [
        {"type": "diff", "path": "src/gone.ts", "start": 1, "end": 6, "side": "old"}]
    assert by_file(document)["src/kept.ts"]["evidence"][0]["side"] == "new"


def test_a_deletion_only_hunk_in_a_modified_file_cites_the_old_lines():
    deletion = make_hunk(10, removed=["a", "b"], context=0)
    _, _, document = finalize([make_file("src/trim.ts", hunks=[make_hunk(1, added=["x"]), deletion])])
    refs = by_file(document)["src/trim.ts"]["evidence"]
    assert {(ref["side"], ref["start"], ref["end"]) for ref in refs} == {("new", 2, 2), ("old", 10, 11)}


def test_a_rename_with_only_removed_lines_cites_the_old_path_on_the_old_side():
    hunk = make_hunk(3, removed=["gone one", "gone two"], context=0)
    files = [make_file("src/new_name.ts", status="renamed", old_path="src/old_name.ts", hunks=[hunk]),
             make_file("src/other.ts", added=["x"])]
    _, _, document = finalize(files)
    node = by_file(document)["src/new_name.ts"]
    assert node["evidence"] == [{"type": "diff", "path": "src/old_name.ts", "start": 3, "end": 4, "side": "old"}]
    assert node["description"].startswith("Renamed from src/old_name.ts, +0/-2")


def test_a_rename_with_new_lines_cites_the_new_path():
    _, _, document = finalize([make_file("src/b.ts", status="renamed", old_path="src/a.ts", added=["x"], removed=["y"])])
    assert [(ref["path"], ref["side"]) for ref in by_file(document)["src/b.ts"]["evidence"]] == [("src/b.ts", "new")]


def test_the_old_path_is_not_used_when_another_changed_file_has_it():
    hunk = make_hunk(3, removed=["gone"], context=0)
    files = [make_file("src/b.ts", status="renamed", old_path="src/a.ts", hunks=[hunk]),
             make_file("src/a.ts", status="added", added=["fresh"])]
    _, _, document = finalize(files)
    assert by_file(document)["src/b.ts"]["evidence"][0]["path"] == "src/b.ts"


def test_files_without_hunk_data_get_no_row_and_a_gap():
    files = [make_file("src/real.ts", added=["x"]), make_file("src/empty.ts", no_hunks=True),
             make_file("src/also.ts", no_hunks=True)]
    _, draft, document = finalize(files)
    assert [node["files"] for node in document["nodes"]] == [["src/real.ts"]]
    assert any(gap.startswith("2 changed files with no usable hunk data") for gap in draft["gaps"])
    _, _, one = build([make_file("src/real.ts", added=["x"]), make_file("src/empty.ts", no_hunks=True)])
    assert any("1 changed file with no usable hunk data cannot be cited and is not shown" in gap for gap in one["gaps"])


def test_a_group_whose_files_have_no_hunks_is_left_out():
    files = [make_file("web/a.ts", added=["x"]), make_file("api/b.ts", no_hunks=True)]
    _, draft, document = finalize(files)
    assert [group["id"] for group in document["groups"]] == ["web"]


def test_docs_only_and_empty_prs_give_no_draft():
    assert build([make_file("README.md", added=["x"]), make_file("docs/guide.md", added=["y"])])[2] is None
    assert build([])[2] is None
    assert build([make_file("src/a.ts", no_hunks=True)])[2] is None
    assert build([make_file("package-lock.json", added=["x"])])[2] is None


def test_a_pr_that_only_deletes_still_has_a_draft():
    _, _, document = finalize([make_file(f"src/old{index}.ts", status="deleted", removed=source_lines(3))
                               for index in range(5)])
    assert {node["change"] for node in document["nodes"]} == {"removed"}
    assert all(ref["side"] == "old" for node in document["nodes"] for ref in node["evidence"])


def test_deletion_heavy_and_rename_heavy_prs_finalize():
    deleted = [make_file(f"legacy/mod{index % 4}/f{index}.ts", status="deleted", removed=source_lines(5 + index % 3))
               for index in range(12)]
    renamed = [make_file(f"src/new{index % 5}/f{index}.ts", status="renamed", old_path=f"src/old{index % 5}/f{index}.ts",
                         hunks=[make_hunk(1, added=["a"], removed=["b"])] if index % 2 else
                         [make_hunk(1, removed=["gone"], context=0)])
               for index in range(12)]
    _, _, document = finalize(deleted + renamed + [make_file("src/live.ts", added=["x"])])
    assert {ref["side"] for node in document["nodes"] for ref in node["evidence"]} == {"new", "old"}
    assert any(ref["path"].startswith("src/old") for node in document["nodes"] for ref in node["evidence"])


# --- routine row -----------------------------------------------------------------------------

def test_routine_files_become_one_row_per_group_without_evidence():
    files = [make_file("web/src/app.ts", added=source_lines(5)),
             make_file("web/package-lock.json", added=source_lines(50)),
             make_file("web/src/api.generated.ts", added=source_lines(5)),
             make_file("web/dist/bundle.js", added=source_lines(5)),
             make_file("api/server.go", added=source_lines(5))]
    _, draft, document = finalize(files)
    routine = [node for node in document["nodes"] if node["kind"] == "routine"]
    assert len(routine) == 1
    row = routine[0]
    assert row["label"] == "3 routine files" and row["evidence"] == [] and row["group"] == "web"
    assert row["files"] == ["web/dist/bundle.js", "web/package-lock.json", "web/src/api.generated.ts"]
    assert any(gap.startswith("3 routine files") for gap in draft["gaps"])
    assert [node["files"] for node in document["nodes"] if node["kind"] == "file" and node["group"] == "web"] == [
        ["web/src/app.ts"]]


def test_a_routine_row_lists_at_most_fifteen_files_and_costs_one_slot():
    files = [make_file(f"web/src/f{index}.ts", added=source_lines(5 + index)) for index in range(12)]
    files += [make_file(f"web/src/vendor/lib{index}.js", added=["x"]) for index in range(20)]
    _, _, draft = build(files)
    routine = next(node for node in draft["nodes"] if node["kind"] == "routine")
    assert routine["label"] == "20 routine files" and len(routine["files"]) == 15
    rows = [node for node in draft["nodes"] if node["group"] == routine["group"]]
    assert len(rows) == 12 and routine in rows  # 12 rows for this size: 11 files and the routine row


# --- edges -----------------------------------------------------------------------------------

def test_import_edges_cite_the_import_line_with_the_right_side():
    changed = make_hunk(1, added=["import { c } from '../api/c';"], removed=["import { b } from '../api/b';"])
    files = [make_file("web/a.ts", hunks=[changed]), make_file("api/b.ts", added=["x"]), make_file("api/c.ts", added=["x"])]
    _, _, document = finalize(files)
    ids = {node["files"][0]: node["id"] for node in document["nodes"]}
    edges = {(edge["source"], edge["target"]): edge for edge in document["edges"]}
    new = edges[(ids["web/a.ts"], ids["api/c.ts"])]
    old = edges[(ids["web/a.ts"], ids["api/b.ts"])]
    assert new["claim"] == "a.ts imports c.ts." and new["change"] == "added" and new["label"] == "imports"
    assert new["evidence"] == [{"type": "diff", "path": "web/a.ts", "start": 2, "end": 2, "side": "new"}]
    assert old["claim"] == "a.ts imported b.ts." and old["change"] == "removed"
    assert old["evidence"] == [{"type": "diff", "path": "web/a.ts", "start": 2, "end": 2, "side": "old"}]


def test_a_removed_import_of_a_deleted_file_is_an_old_side_edge():
    files = [make_file("src/a.ts", hunks=[make_hunk(1, removed=["import { b } from './b';"], context=1)]),
             make_file("src/b.ts", status="deleted", removed=["export const b = 1;"])]
    _, _, document = finalize(files)
    assert [(edge["claim"], edge["change"]) for edge in document["edges"]] == [("a.ts imported b.ts.", "removed")]


def test_an_import_present_on_both_sides_is_one_modified_edge_with_both_citations():
    hunk = make_hunk(1, added=["import { b, extra } from './b';"], removed=["import { b } from './b';"])
    _, _, document = finalize([make_file("src/a.ts", hunks=[hunk]), make_file("src/b.ts", added=["x"])])
    (edge,) = document["edges"]
    assert edge["claim"] == "a.ts imports b.ts." and edge["change"] == "modified"
    assert [ref["side"] for ref in edge["evidence"]] == ["new", "old"]


def test_multi_language_imports_become_edges():
    files = [
        make_file("web/app/page.tsx", added=["import { api } from './client';"]),
        make_file("web/app/client.ts", added=["export const api = 1;"]),
        make_file("svc/main.py", added=["from . import util", "import svc.models"]),
        make_file("svc/util.py", added=["x = 1"]),
        make_file("svc/models.py", added=["y = 2"]),
        make_file("internal/handler/handler.go", added=['import "github.com/acme/repo/internal/store"']),
        make_file("internal/store/store.go", added=["package store"]),
    ]
    _, _, document = finalize(files)
    ids = {node["files"][0]: node["id"] for node in document["nodes"]}
    pairs = {(edge["source"], edge["target"]) for edge in document["edges"]}
    assert pairs == {(ids["web/app/page.tsx"], ids["web/app/client.ts"]), (ids["svc/main.py"], ids["svc/util.py"]),
                     (ids["svc/main.py"], ids["svc/models.py"]),
                     (ids["internal/handler/handler.go"], ids["internal/store/store.go"])}


def test_an_import_on_an_unchanged_context_line_is_an_unchanged_edge_that_validates():
    context = {"old_start": 1, "old_lines": 3, "new_start": 1, "new_lines": 4, "section": "", "lines": [
        {"kind": "ctx", "text": "import { b } from './b';", "old_no": 1, "new_no": 1},
        {"kind": "add", "text": "const x = 1;", "new_no": 2},
        {"kind": "ctx", "text": "export const a = 1;", "old_no": 2, "new_no": 3},
        {"kind": "ctx", "text": "export const c = 1;", "old_no": 3, "new_no": 4}]}
    files = [make_file("src/a.ts", hunks=[context]), make_file("src/b.ts", added=["x"])]
    snapshot, _, document = finalize(files)
    (edge,) = document["edges"]
    assert edge["evidence"] == [{"type": "diff", "path": "src/a.ts", "start": 1, "end": 1, "side": "new"}]
    assert edge["change"] == "unchanged"
    validate_v2(document, HEAD, snapshot)


def test_a_go_import_of_a_multi_file_package_draws_no_file_edge():
    main = ['import (', '\t"github.com/acme/repo/internal/scan"', '\t"github.com/acme/repo/internal/solo"', ')']
    files = [make_file("cmd/app/main.go", added=main),
             make_file("internal/scan/replay.go", added=source_lines(30)),
             make_file("internal/scan/scan.go", added=source_lines(5)),
             make_file("internal/solo/solo.go", added=["package solo"])]
    _, skeleton, _ = build(files)
    assert any(edge["target_file"] is None for edge in skeleton["edges"])  # the skeleton keeps the package link
    _, _, document = finalize(files)
    ids = {node["files"][0]: node["id"] for node in document["nodes"]}
    assert {(e["source"], e["target"]) for e in document["edges"]} == {(ids["cmd/app/main.go"], ids["internal/solo/solo.go"])}
    assert all("replay.go" not in e["claim"] and "scan.go" not in e["claim"] for e in document["edges"])


def test_edges_to_files_without_a_row_are_dropped():
    # b.ts is hunk-less (no row), c.ts is a routine file, d.ts is beyond the row limit
    files = [make_file("src/a.ts", added=["import b from './b';", "import c from './c';", "import d from './d';"]),
             make_file("src/b.ts", no_hunks=True), make_file("src/c.ts", added=["x"])]
    _, _, document = finalize(files)
    assert [(edge["claim"]) for edge in document["edges"]] == ["a.ts imports c.ts."]
    _, _, draft = build([make_file("src/a.ts", added=["import b from './b';"]), make_file("src/b.ts", no_hunks=True)])
    assert draft["edges"] == []


def test_edges_are_unique_and_never_invented():
    files = [make_file("src/a.ts", added=[f"import x{i} from './b';" for i in range(5)]),
             make_file("src/b.ts", added=["x"])]
    _, _, document = finalize(files)
    assert len(document["edges"]) == 1 and len(document["edges"][0]["evidence"]) == 3
    _, _, draft = build(many_files(12))
    assert draft["edges"] == []


def test_edge_evidence_is_capped_per_side():
    lines = [f"import x{i} from './b';" for i in range(10)]
    _, _, document = finalize([make_file("src/a.ts", added=lines), make_file("src/b.ts", added=["x"])])
    assert len(document["edges"][0]["evidence"]) == 3


# --- caps ------------------------------------------------------------------------------------

def thousand_files():
    files = many_files(1000, lines=8)
    for index in range(0, 1000, 2):
        files[index]["hunks"][0]["lines"].insert(1, {"kind": "add", "text": f"import x from './file{index + 1}';", "new_no": 2})
        files[index]["hunks"][0]["new_lines"] += 1
        files[index]["hunks"][0]["old_lines"] += 0
    return files


def test_a_thousand_files_respect_the_v2_caps_and_stay_fast():
    files = thousand_files()
    snapshot = make_snapshot(files)
    started = time.perf_counter()
    skeleton = build_skeleton(snapshot, HEAD)
    draft = deterministic_v2_draft(skeleton, snapshot, HEAD)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, elapsed
    assert len(draft["groups"]) <= 12 and len(draft["nodes"]) <= 60 and len(draft["edges"]) <= 60
    assert len(draft["groups"]) == 12 and "other" in {group["id"] for group in draft["groups"]}
    assert next(group for group in draft["groups"] if group["id"] == "other")["label"] == "Other changes"
    assert any("merged into Other changes" in gap for gap in draft["gaps"])
    assert any("row limit" in gap for gap in draft["gaps"])
    assert len(draft["gaps"]) <= 5 and all(len(gap) <= 300 for gap in draft["gaps"])
    document, warnings = finalize_v2(draft, HEAD, snapshot, "deterministic")
    assert warnings == [] and len(document["nodes"]) == len(draft["nodes"])
    per_group = {}
    for node in draft["nodes"]:
        per_group[node["group"]] = per_group.get(node["group"], 0) + 1
    assert max(per_group.values()) <= 5


def test_the_edge_cap_keeps_the_best_connected_pairs():
    files = [make_file(f"src/f{index}.ts", added=[f"import a from './f{(index + 1) % 30}';",
                                                  f"import b from './f{(index + 2) % 30}';"] + source_lines(3))
             for index in range(30)]
    _, _, draft = build(files)
    assert 0 < len(draft["edges"]) <= 60
    _, _, document = finalize(files)
    assert len(document["edges"]) == len(draft["edges"])


# --- ids, determinism, odd input -------------------------------------------------------------

def test_ids_match_the_pattern_and_are_unique_even_for_odd_names():
    files = [make_file(path, added=["x"]) for path in (
        "src/same.ts", "lib/same.ts", "a/b/c/d/same.ts", "src/1leading.ts", "src/__init__.py", "src/ünï cödé.ts",
        "src/" + "x" * 120 + ".ts", "src/a b.ts", "src/a-b.ts", "src/a_b.ts", "-weird/-.ts", "9/9.ts")]
    files.append(make_file("a" * 80 + "/" + "b" * 80 + "/file.ts", added=["x"]))
    _, draft, _ = finalize(files)
    for items in (draft["groups"], draft["nodes"], draft["edges"]):
        ids = [item["id"] for item in items]
        assert len(ids) == len(set(ids)) and all(ID.fullmatch(name) and len(name) <= 64 for name in ids)
    assert len(draft["nodes"]) == len(files)


def test_node_ids_do_not_change_when_other_files_are_added():
    base = [make_file("src/a.ts", added=["x"]), make_file("src/b.ts", added=["y"])]
    first = {node["files"][0]: node["id"] for node in build(base)[2]["nodes"]}
    second = {node["files"][0]: node["id"] for node in build(base + [make_file("src/c.ts", added=["z"])])[2]["nodes"]}
    assert all(second[path] == name for path, name in first.items())


def test_the_same_snapshot_gives_byte_identical_drafts():
    files = thousand_files()
    snapshot = make_snapshot(files)
    skeleton = build_skeleton(snapshot, HEAD)
    first = json.dumps(deterministic_v2_draft(skeleton, snapshot, HEAD))
    assert json.dumps(deterministic_v2_draft(skeleton, snapshot, HEAD)) == first
    assert json.dumps(deterministic_v2_draft(build_skeleton(make_snapshot(list(files)), HEAD), snapshot, HEAD)) == first


@pytest.mark.parametrize("snapshot", [
    None, {}, {"pr": None}, {"pr": {"files": None}}, {"pr": {"files": [None, 3, {"path": 5}, {"path": ""}]}},
    {"pr": {"files": [{"path": "a.ts", "status": "added", "hunks": [None, {"lines": 4}, {"new_start": "1"}]}]}},
    {"pr": {"files": [{"path": "a.ts", "hunks": [{"old_start": 1, "old_lines": 1, "new_start": 1, "new_lines": 1,
                                                 "lines": [None, 3, {"kind": 4}, {"kind": "add"}]}]}]}},
])
def test_odd_snapshots_never_raise(snapshot):
    skeleton = build_skeleton(snapshot, HEAD)
    draft = deterministic_v2_draft(skeleton, snapshot, HEAD)
    assert draft is None or isinstance(draft, dict)
    for bad in (None, {}, [], "x", {"groups": 3, "files": None, "docs_only": False}):
        assert deterministic_v2_draft(bad, snapshot, HEAD) is None or isinstance(
            deterministic_v2_draft(bad, snapshot, HEAD), dict)


def test_a_hunk_without_line_text_is_cited_by_its_range():
    hunk = {"old_start": 4, "old_lines": 3, "new_start": 4, "new_lines": 5, "section": "", "lines": []}
    _, _, document = finalize([make_file("src/a.ts", hunks=[hunk])])
    assert document["nodes"][0]["evidence"] == [{"type": "diff", "path": "src/a.ts", "start": 4, "end": 8, "side": "new"}]


# --- writer ----------------------------------------------------------------------------------

class Log(io.StringIO):
    pass


class BrokenLog:
    def write(self, message):
        raise OSError("log closed")


def document_for(files=None):
    snapshot, _, draft = build(files or many_files(12))
    return finalize_v2(draft, HEAD, snapshot, "deterministic")[0]


def artifact(tmp_path):
    folder = tmp_path / ".paircli" / "pr-7" / HEAD
    folder.mkdir(parents=True)
    return folder


def status_file(folder):
    return folder / "diagnostics" / "architecture-v2-status.json"


def test_write_architecture_v2_saves_the_document_and_the_status(tmp_path):
    folder = artifact(tmp_path)
    document = document_for()
    log = Log()
    path = write_architecture_v2(folder, document, log, tier="deterministic", reason="llm unavailable",
                                 llm_calls={"attempts": 2})
    assert path == folder / "generated_artifacts" / "architecture.v2.json"
    assert json.loads(path.read_text(encoding="utf-8")) == document
    validate_v2(json.loads(path.read_text()), HEAD, make_snapshot(many_files(12)))
    assert json.loads(status_file(folder).read_text()) == {
        "tier": "deterministic", "reason": "llm unavailable", "generated_by": "deterministic",
        "counts": {"groups": len(document["groups"]), "nodes": len(document["nodes"]),
                   "edges": len(document["edges"]), "steps": 0},
        "llm_calls": {"attempts": 2}}
    assert "architecture.v2.json" in log.getvalue()
    assert not list((folder / "generated_artifacts").glob("*.tmp")) and not list((folder / "diagnostics").glob("*.tmp"))
    assert not (folder / "generated_artifacts" / "architecture.json").exists()


def test_status_defaults_and_llm_tier(tmp_path):
    folder = artifact(tmp_path)
    document = dict(document_for(), generated_by="llm")
    write_architecture_v2(folder, document, Log(), tier="llm", reason="llm diagram validated")
    status = json.loads(status_file(folder).read_text())
    assert status["tier"] == "llm" and status["generated_by"] == "llm" and status["llm_calls"] == {}


def test_a_second_write_replaces_the_first_atomically(tmp_path):
    folder = artifact(tmp_path)
    first, second = document_for(many_files(12)), document_for([make_file("src/a.ts", added=["x"])])
    write_architecture_v2(folder, first, Log(), tier="deterministic", reason="a")
    target = write_architecture_v2(folder, second, Log(), tier="deterministic", reason="b")
    assert json.loads(target.read_text()) == second
    assert json.loads(status_file(folder).read_text())["reason"] == "b"


def test_a_failed_replace_leaves_the_old_document_and_no_temp_file(tmp_path, monkeypatch):
    folder = artifact(tmp_path)
    old = document_for([make_file("src/a.ts", added=["x"])])
    target = write_architecture_v2(folder, old, Log(), tier="deterministic", reason="a")
    before = target.read_bytes()

    def fail(self, other):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        write_architecture_v2(folder, document_for(many_files(12)), Log(), tier="deterministic", reason="b")
    monkeypatch.undo()
    assert target.read_bytes() == before
    assert not list((folder / "generated_artifacts").glob("*.tmp"))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlinks_are_refused_without_writing_through_them(tmp_path):
    folder = artifact(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    document = document_for()
    (folder / "generated_artifacts").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError):
        write_architecture_v2(folder, document, Log(), tier="deterministic", reason="x")
    assert list(elsewhere.iterdir()) == []
    (folder / "generated_artifacts").unlink()

    (folder / "generated_artifacts").mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text("keep")
    (folder / "generated_artifacts" / "architecture.v2.json").symlink_to(victim)
    with pytest.raises(ValueError):
        write_architecture_v2(folder, document, Log(), tier="deterministic", reason="x")
    assert victim.read_text() == "keep"
    (folder / "generated_artifacts" / "architecture.v2.json").unlink()

    linked = tmp_path / "linked"
    linked.symlink_to(folder, target_is_directory=True)
    with pytest.raises(ValueError):
        write_architecture_v2(linked, document, Log(), tier="deterministic", reason="x")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_symlinked_diagnostics_folder_costs_only_the_status_not_the_document(tmp_path):
    folder = artifact(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (folder / "diagnostics").symlink_to(elsewhere, target_is_directory=True)
    log = Log()
    path = write_architecture_v2(folder, document_for(), log, tier="deterministic", reason="x")
    assert path.is_file() and list(elsewhere.iterdir()) == []
    assert "status not saved" in log.getvalue()


def test_bad_tier_and_oversized_documents_are_refused(tmp_path):
    folder = artifact(tmp_path)
    with pytest.raises(ValueError):
        write_architecture_v2(folder, document_for(), Log(), tier="mystery", reason="x")
    with pytest.raises(ValueError):
        write_architecture_v2(folder, {"title": "x" * (MAX_BYTES + 1)}, Log(), tier="llm", reason="x")
    assert not (folder / "generated_artifacts" / "architecture.v2.json").exists()


def test_a_broken_log_never_raises(tmp_path):
    folder = artifact(tmp_path)
    path = write_architecture_v2(folder, document_for(), BrokenLog(), tier="deterministic", reason="x")
    assert path.is_file() and status_file(folder).is_file()
    write_no_architecture_v2(folder, "docs-only change", BrokenLog())


def test_remove_stale_architecture_v2_removes_only_the_v2_file(tmp_path):
    folder = artifact(tmp_path)
    write_architecture_v2(folder, document_for(), Log(), tier="deterministic", reason="x")
    (folder / "generated_artifacts" / "architecture.json").write_text("{}")
    remove_stale_architecture_v2(folder)
    remove_stale_architecture_v2(folder)  # idempotent
    assert not (folder / "generated_artifacts" / "architecture.v2.json").exists()
    assert (folder / "generated_artifacts" / "architecture.json").exists()
    remove_stale_architecture_v2(tmp_path / "missing")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_remove_stale_architecture_v2_does_not_follow_a_symlinked_folder(tmp_path):
    folder = artifact(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "architecture.v2.json").write_text("keep")
    (folder / "generated_artifacts").symlink_to(elsewhere, target_is_directory=True)
    remove_stale_architecture_v2(folder)
    assert (elsewhere / "architecture.v2.json").read_text() == "keep"


def test_write_no_architecture_v2_removes_the_old_file_and_records_none(tmp_path):
    folder = artifact(tmp_path)
    write_architecture_v2(folder, document_for(), Log(), tier="deterministic", reason="x")
    log = Log()
    assert write_no_architecture_v2(folder, "docs-only change", log, llm_calls={"attempts": 1}) is None
    assert not (folder / "generated_artifacts" / "architecture.v2.json").exists()
    assert json.loads(status_file(folder).read_text()) == {
        "tier": "none", "reason": "docs-only change", "generated_by": None,
        "counts": {"groups": 0, "nodes": 0, "edges": 0, "steps": 0}, "llm_calls": {"attempts": 1}}
    assert "docs-only change" in log.getvalue()
    write_no_architecture_v2(folder, "again")  # the log is optional
    assert json.loads(status_file(folder).read_text())["reason"] == "again"


def test_the_v2_document_ships_in_the_zip_and_its_status_does_not(tmp_path):
    folder = artifact(tmp_path)
    (folder / "summary.md").write_text("summary")
    write_architecture_v2(folder, document_for(), Log(), tier="deterministic", reason="x")
    prefix = f".paircli/pr-7/{HEAD}"
    with zipfile.ZipFile(create_pr_archive(tmp_path, folder)) as archive:
        names = archive.namelist()
        assert f"{prefix}/generated_artifacts/architecture.v2.json" in names
        assert not any("architecture-v2-status" in name or "diagnostics" in name for name in names)
        shipped = json.loads(archive.read(f"{prefix}/generated_artifacts/architecture.v2.json"))
    assert shipped["schema_version"] == 2 and shipped["generated_by"] == "deterministic"
