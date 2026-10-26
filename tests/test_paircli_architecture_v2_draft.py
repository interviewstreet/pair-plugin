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
from snapshot_factory import (
    HEAD, deleted_templates, deletion_heavy_pr, make_file, make_hunk, make_snapshot, many_files, repeated_edits,
    source_lines, version_bumps,
)

ID = re.compile(IDENTIFIER)
LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"  # distinct, digit-free text per file


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
    assert document["nodes"][0]["stats"] == {"additions": 4, "deletions": 1, "files": 1, "api_added": 0, "api_removed": 0}
    assert draft["gaps"][-1].startswith("Generated deterministically")


@pytest.mark.parametrize("count", [12, 34, 253, 1000])
def test_synthetic_prs_of_every_size_finalize_with_nothing_dropped(count):
    snapshot, _, document = finalize(many_files(count))
    validate_v2(document, HEAD, snapshot)
    assert len(json.dumps(document, ensure_ascii=False).encode()) < MAX_BYTES


def test_draft_shape_keeps_to_the_model_writable_fields():
    _, _, draft = build(many_files(12, unique=True))
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
    files = [make_file(f"web/src/f{index}.ts", added=[f"const {chr(97 + index)}{line} = 1;" for line in range(5 + index)]) for index in range(12)]
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
    files = many_files(1000, lines=8, unique=True)
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
                                                  f"import b from './f{(index + 2) % 30}';",
                                                  f"const own = '{LETTERS[index]}';"] + source_lines(3))
             for index in range(30)]  # a line of its own each, so the files are not one repeated change
    _, _, draft = build(files)
    assert 0 < len(draft["edges"]) <= 60
    _, _, document = finalize(files)
    assert len(document["edges"]) == len(draft["edges"])


# --- ids, determinism, odd input -------------------------------------------------------------

def test_ids_match_the_pattern_and_are_unique_even_for_odd_names():
    files = [make_file(path, added=[f"const own = '{LETTERS[number]}';"]) for number, path in enumerate((
        "src/same.ts", "lib/same.ts", "a/b/c/d/same.ts", "src/1leading.ts", "src/__init__.py", "src/ünï cödé.ts",
        "src/" + "x" * 120 + ".ts", "src/a b.ts", "src/a-b.ts", "src/a_b.ts", "-weird/-.ts", "9/9.ts"))]
    files.append(make_file("a" * 80 + "/" + "b" * 80 + "/file.ts", added=["const own = 'z';"]))
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


# --- pattern rows ----------------------------------------------------------------------------

def patterns_of(document):
    return [node for node in document["nodes"] if node["kind"] == "pattern"]


def covered(document):
    return sum(node["pattern"]["count"] for node in patterns_of(document))


def test_a_deletion_heavy_pr_collapses_into_pattern_rows_and_collapsed_edges():
    files = deletion_heavy_pr(templates=40, features=9)
    _, draft, document = finalize(files)
    rows = {node["id"]: node for node in document["nodes"]}
    assert len(rows) == 3 and len(rows) < len(files) / 10
    deleted = next(node for node in patterns_of(document) if node["pattern"]["family"] == "same-change"
                   and node["change"] == "removed")
    assert deleted["pattern"]["count"] == 40 and len(deleted["pattern"]["members"]) == 40
    assert deleted["label"] == "40 files: deleted"
    assert deleted["description"].startswith("40 files with the same change: deleted. Each removes the same lines")
    assert len(deleted["files"]) == 12 and set(deleted["files"]) <= set(deleted["pattern"]["members"])
    assert {ref["side"] for ref in deleted["evidence"]} == {"old"} and len(deleted["evidence"]) == 12
    assert deleted["stats"]["files"] == 40 and deleted["stats"]["deletions"] == 160 and deleted["stats"]["additions"] == 0
    assert (deleted["change"], deleted["state"]) == ("removed", "changed")
    assert not any(node["files"] == [path] for node in document["nodes"] for path in deleted["pattern"]["members"])
    index = next(node for node in document["nodes"] if node["files"] == ["src/legacy/index.ts"])
    assert len(document["edges"]) == 2
    collapsed = next(edge for edge in document["edges"] if edge["source"] == index["id"])
    assert collapsed["target"] == deleted["id"] and collapsed["label"] == "imports (40)"
    assert collapsed["claim"] == 'index.ts imported 40 of the 40 files in "40 files: deleted".'
    assert len(collapsed["evidence"]) == 3 and {ref["side"] for ref in collapsed["evidence"]} == {"old"}
    assert collapsed["change"] == "removed"
    features = next(node for node in patterns_of(document) if node is not deleted)
    assert features["pattern"]["count"] == 9
    second = next(edge for edge in document["edges"] if edge["source"] == features["id"])
    assert second["target"] == index["id"] and second["label"] == "imports (9)"
    assert second["claim"] == '9 of the 9 files in "9 files: same edit" imported index.ts.'
    assert any("49 changed files folded into 2 pattern rows" in gap for gap in document["gaps"])
    assert len(draft["nodes"]) == 3


def test_a_mechanical_bump_in_one_directory_is_one_pattern_row():
    files = [make_file(f"packages/bump/pkg{index}.json", start=3, removed=['  "version": "1.2.3",'],
                       added=['  "version": "1.3.0",']) for index in range(40)]
    _, _, document = finalize(files)
    assert len(document["nodes"]) == 1
    node = document["nodes"][0]
    assert node["kind"] == "pattern" and node["pattern"]["count"] == 40
    assert node["label"] == "40 files: same edit" and node["stats"]["additions"] == 40 == node["stats"]["deletions"]
    assert node["description"].startswith("40 files with the same change: modified. Each makes the same edit")


def test_a_bump_across_package_directories_is_one_pattern_row():
    # The directory grouping spreads 40 package.json files over many groups; the same change is
    # clustered PR-wide and the pattern row sits in the group that holds most of its members.
    files = version_bumps(40)
    snapshot, _, document = finalize(files)
    skeleton = build_skeleton(snapshot, HEAD)
    assert len(skeleton["groups"]) > 3, "the layout is flat: many directory groups"
    (row,) = document["nodes"]
    assert row["kind"] == "pattern" and row["pattern"]["count"] == 40 == covered(document)
    assert len(document["groups"]) == 1 and document["groups"][0]["id"] == row["group"]
    assert "Spans 40 directories." in row["description"]
    assert document["groups"][0]["path"] == "packages", "common directory of the members"
    validate_v2(document, HEAD, snapshot)


def test_flat_monorepo_bumps_never_make_one_row_per_directory():
    files = [make_file(f"packages/pkg{index:02d}/package.json", start=3, removed=['  "version": "1.2.3",'],
                       added=['  "version": "1.3.0",']) for index in range(40)]
    _, _, document = finalize(files)
    assert [node["kind"] for node in document["nodes"]] == ["pattern"]
    assert document["nodes"][0]["stats"]["files"] == 40


def test_many_modified_files_with_three_repeated_edits_give_three_pattern_rows():
    files = repeated_edits(480, variants=3, directory="src/modules")
    for index, file in enumerate(files):  # one flat directory: one group
        file["path"] = f"src/modules/mod{index}.ts"
    _, _, document = finalize(files)
    patterns = patterns_of(document)
    assert len(document["nodes"]) == 3 == len(patterns)
    assert sorted(node["pattern"]["count"] for node in patterns) == [160, 160, 160]
    assert len({node["pattern"]["signature"] for node in patterns}) == 3
    assert len({member for node in patterns for member in node["pattern"]["members"]}) == 480
    assert all(len(node["files"]) == 12 and len(node["evidence"]) == 12 for node in patterns)


def flat_package_edits(variants: int = 3, packages: int = 40, per_package: int = 12) -> list[dict]:
    """`per_package` src/modK.ts files in each of `packages` packages; K picks one of `variants` edits."""
    names = ["Logger", "Tracer", "Meter", "Clock", "Cache"]
    return [make_file(f"packages/pkg{package:02d}/src/mod{number}.ts", start=2,
                      removed=[f"import {{ log }} from '{names[number % variants]}';  // v1"],
                      added=[f"import {{ log }} from '{names[number % variants]}2';  // v{package}"])
            for package in range(packages) for number in range(per_package)]


def test_repeated_edits_spread_over_packages_give_one_row_per_edit_kind():
    files = flat_package_edits()
    assert len(files) == 480
    snapshot, _, document = finalize(files)
    assert len(build_skeleton(snapshot, HEAD)["groups"]) > 3
    patterns = patterns_of(document)
    assert len(document["nodes"]) == 3 == len(patterns)
    assert sorted(node["pattern"]["count"] for node in patterns) == [160, 160, 160]
    assert len({member for node in patterns for member in node["pattern"]["members"]}) == 480
    assert all("Spans 40 directories." in node["description"] for node in patterns)
    assert all(node["group"] in {group["id"] for group in document["groups"]} for node in patterns)
    assert len({group["id"] for group in document["groups"]}) == len(document["groups"]) <= 3
    validate_v2(document, HEAD, snapshot)


def test_pattern_goes_to_the_group_with_most_members_and_leaves_no_empty_groups():
    # 5 members in src/a, 3 in src/b, 1 in src/c: the pattern is src/a's; b and c keep nothing else.
    members = ([f"src/a/m{index}.ts" for index in range(5)] + [f"src/b/m{index}.ts" for index in range(3)]
               + ["src/c/m0.ts"])
    files = [make_file(path, start=2, removed=["const mode = 'old';"], added=[f"const mode = 'new{number}';"])
             for number, path in enumerate(members)]
    files += [make_file("src/d/x.ts", added=["const solo = 'd';"])]
    snapshot, skeleton, draft = build(files)
    holders = [group for group in skeleton["groups"] if group["patterns"]]
    assert [group["path"] for group in holders] == ["src/a"] and holders[0]["patterns"][0]["count"] == 9
    assert all(not group["patterns"] for group in skeleton["groups"] if group is not holders[0])
    # The other directories' files stay in their `files` (the v1 diagram reads them) and claim `in_pattern`.
    assert any(path in group["files"] for group in skeleton["groups"] if group["path"] == "src/b" for path in members)
    document, warnings = finalize_v2(draft, HEAD, snapshot, "deterministic")
    assert warnings == []
    assert sorted(node["kind"] for node in document["nodes"]) == ["file", "pattern"]
    pattern = patterns_of(document)[0]
    assert pattern["group"] == "src-a" and "Spans 3 directories." in pattern["description"]
    assert {group["id"] for group in document["groups"]} == {"src-a", "src-d"}


def test_a_tie_between_groups_goes_to_the_smallest_group_id():
    members = [f"src/{name}/m{index}.ts" for name in ("zeta", "alpha", "mid") for index in range(2)]
    files = [make_file(path, start=2, removed=["const mode = 'old';"], added=[f"const mode = 'new{number}';"])
             for number, path in enumerate(members)]
    _, skeleton, draft = build(files)
    holders = [group for group in skeleton["groups"] if group["patterns"]]
    assert len(holders) == 1 and holders[0]["id"] == "src-alpha"
    assert [node["group"] for node in draft["nodes"]] == ["src-alpha"]


def test_two_directories_with_different_repeated_changes_keep_two_patterns():
    one = [make_file(f"src/alpha/f{index}.ts", status="deleted", removed=[f"// gone {index}", "export const A = 1;"])
           for index in range(3)]
    two = [make_file(f"src/beta/f{index}.ts", start=2, removed=["const mode = 'old';"],
                     added=[f"const mode = 'new{index}';"]) for index in range(3)]
    _, _, document = finalize(one + two)
    patterns = patterns_of(document)
    assert len(patterns) == 2 == len(document["nodes"]) and patterns[0]["group"] != patterns[1]["group"]
    assert all("Spans" not in node["description"] for node in patterns)


def test_pattern_clusters_are_deterministic_and_fast_across_a_thousand_flat_files():
    files = flat_package_edits(variants=4, packages=40, per_package=25)
    snapshot = make_snapshot(files)
    started = time.perf_counter()
    skeleton = build_skeleton(snapshot, HEAD)
    draft = deterministic_v2_draft(skeleton, snapshot, HEAD)
    document, warnings = finalize_v2(draft, HEAD, snapshot, "deterministic")
    assert time.perf_counter() - started < 2.0
    assert warnings == [] and len(patterns_of(document)) == 4 and covered(document) == 1000
    assert json.dumps(draft) == json.dumps(deterministic_v2_draft(build_skeleton(snapshot, HEAD), snapshot, HEAD))


def test_a_pattern_larger_than_500_files_is_split_into_several_rows():
    files = [make_file(f"src/bulk/m{index}.ts", start=2, removed=["export const old = 1;"],
                       added=["export const old = 2;"]) for index in range(1100)]
    _, _, document = finalize(files)
    sizes = sorted(node["pattern"]["count"] for node in patterns_of(document))
    assert all(3 <= size <= 500 for size in sizes) and sum(sizes) == 1100
    assert len(document["nodes"]) <= 4 and len({node["pattern"]["signature"] for node in patterns_of(document)}) == 1


def test_tests_docs_and_config_fold_by_group_and_get_no_edges():
    source = [make_file("src/app/service.ts", added=["export function run() {}"] + source_lines(3)),
              make_file("src/app/store.ts", added=["export const store = {};"] + source_lines(4, "const other"))]
    tests = [make_file(f"src/app/__tests__/case{index}.test.ts", added=[f"import {{ run }} from '../service';",
                                                                      f"it('case {LETTERS[index]}', () => {{}});"])
             for index in range(5)]
    docs = [make_file(f"src/app/docs/page{index}.md", added=[f"# page {LETTERS[index]}"]) for index in range(4)]
    config = [make_file(f"src/app/conf{index}.json", added=[f'{{"k{LETTERS[index]}": true}}']) for index in range(3)]
    _, _, document = finalize(source + tests + docs + config)
    kinds = {node["label"]: node for node in patterns_of(document)}
    assert set(kinds) == {"5 test files", "4 doc files", "3 config files"}
    assert kinds["5 test files"]["description"] == "5 test files changed in this area. Together +10/-0 lines."
    assert {node["pattern"]["family"] for node in patterns_of(document)} == {"tests", "docs", "config"}
    assert all("signature" not in node["pattern"] for node in patterns_of(document))
    assert len(document["nodes"]) == 5  # two source files and three folded rows
    pattern_ids = {node["id"] for node in patterns_of(document)}
    assert not [edge for edge in document["edges"] if {edge["source"], edge["target"]} & pattern_ids]
    assert any("12 changed files folded into 3 pattern rows" in gap for gap in document["gaps"])


def test_only_the_files_that_share_a_signature_are_folded():
    shared = [make_file(f"src/mix/a{index}.ts", start=2, removed=["const mode = 'old';"],
                        added=[f"const mode = 'new{index}';"]) for index in range(6)]
    others = [make_file(f"src/mix/b{index}.ts", start=2, removed=[f"const x{LETTERS[index]} = 1;"],
                        added=[f"const y{LETTERS[index]} = 2;"]) for index in range(4)]
    _, _, document = finalize(shared + others)
    assert [node["pattern"]["count"] for node in patterns_of(document)] == [6]
    assert sorted(node["files"][0] for node in document["nodes"] if node["kind"] == "file") == sorted(
        f"src/mix/b{index}.ts" for index in range(4))
    assert len(document["nodes"]) == 5


def test_the_same_change_in_two_groups_is_one_pattern_in_the_larger_group():
    one = [make_file(f"src/alpha/f{index}.ts", status="deleted", removed=[f"// gone {index}", "export const A = 1;"])
           for index in range(5)]
    two = [make_file(f"src/beta/f{index}.ts", status="deleted", removed=[f"// gone {index}", "export const A = 1;"])
           for index in range(4)]
    files = one + two + [make_file("src/gamma/app.ts", added=source_lines(5))]
    _, skeleton, _ = build(files)
    assert {group["path"]: [pattern["count"] for pattern in group["patterns"]] for group in skeleton["groups"]
            if group["patterns"]} == {"src/alpha": [9]}
    _, _, document = finalize(files)
    (pattern,) = patterns_of(document)
    assert pattern["pattern"]["count"] == 9 and "Spans 2 directories." in pattern["description"]


def test_a_pattern_row_wins_budget_over_a_small_file_and_modified_source_over_added_boilerplate():
    files = ([make_file(f"src/big/gen{index}.ts", "added", added=[f"export const g{LETTERS[index]} = 1;"] * 1)
              for index in range(12)]
             + [make_file("src/big/core.ts", added=["export function core() {}"], removed=["function core() {}"])]
             + deleted_templates(5, "src/big"))
    _, _, document = finalize(files)
    labels = [node["label"] for node in document["nodes"]]
    assert "5 files: deleted" in labels, "the repeated deletion keeps its row"
    assert "core.ts" in labels, "an edited source file outranks added boilerplate when rows run out"
    assert len([node for node in document["nodes"] if node["group"] == document["nodes"][0]["group"]]) <= 12


def test_pattern_members_may_be_renamed_and_hunkless_files_are_left_out():
    renamed = [make_file(f"src/new/r{index}.ts", "renamed", old_path=f"src/old/r{index}.ts", start=2,
                         removed=["import a from './a';"], added=["import a from './b';"]) for index in range(4)]
    hunkless = [make_file(f"src/new/h{index}.ts", "renamed", old_path=f"src/old/h{index}.ts", no_hunks=True)
                for index in range(2)]
    _, _, document = finalize(renamed + hunkless)
    (node,) = patterns_of(document)
    assert node["pattern"]["count"] == 4 and node["change"] == "modified" and node["label"] == "4 files: renamed and edited"
    assert not any(path.startswith("src/new/h") for path in node["pattern"]["members"])
    assert any("no usable hunk data" in gap for gap in document["gaps"])


def test_a_pattern_row_is_one_row_of_the_group_budget():
    files = [make_file(f"src/one/m{index}.ts", "deleted", removed=[f"// {index}", "export {};"]) for index in range(30)]
    files += [make_file(f"src/one/own{LETTERS[index]}.ts", added=[f"const {LETTERS[index]}own = 'x';"] * 2)
              for index in range(30)]
    _, skeleton, draft = build(files)
    rows = [node for node in draft["nodes"] if node["group"] == draft["nodes"][0]["group"]]
    assert len(rows) <= skeleton["budget"]["max_rows_per_group"]
    assert sum(node["kind"] == "pattern" for node in draft["nodes"]) == 1


def test_pattern_drafts_are_deterministic_and_fast_for_a_thousand_files():
    files = (version_bumps(150, "pkgs") + repeated_edits(300, variants=4) + deleted_templates(200)
             + many_files(350, lines=4))
    for index, file in enumerate(files[650:]):
        file["hunks"][0]["lines"][1]["text"] = f"const unique_{LETTERS[index % 52]}{LETTERS[index // 52]} = 1;"
    snapshot = make_snapshot(files)
    started = time.perf_counter()
    skeleton = build_skeleton(snapshot, HEAD)
    draft = deterministic_v2_draft(skeleton, snapshot, HEAD)
    document, warnings = finalize_v2(draft, HEAD, snapshot, "deterministic")
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, elapsed
    assert warnings == [] and len(document["nodes"]) <= 60 and len(document["edges"]) <= 60
    assert covered(document) > 400
    again = deterministic_v2_draft(build_skeleton(snapshot, HEAD), snapshot, HEAD)
    assert json.dumps(draft) == json.dumps(again)
    assert len(json.dumps(document).encode()) <= MAX_BYTES


def test_pattern_edges_between_two_pattern_rows_collapse_into_one():
    callers = [make_file(f"src/callers/c{index}.ts", start=2, added=[f"import {{ x }} from '../callees/e{index}';"],
                         removed=["// before"]) for index in range(5)]
    callees = [make_file(f"src/callees/e{index}.ts", "added", added=["export const x = 1;", f"// callee {index}"])
               for index in range(5)]
    _, _, document = finalize(callers + callees)
    assert len(patterns_of(document)) == 2 and len(document["edges"]) == 1
    edge = document["edges"][0]
    assert edge["label"] == "imports (5)" and len(edge["evidence"]) == 3 and edge["change"] == "added"
    assert edge["claim"] == '5 of the 5 files in "5 files: same edit" import 5 of the 5 files in "5 files: added".'
    assert {document["nodes"][0]["id"], document["nodes"][1]["id"]} == {edge["source"], edge["target"]}


def test_an_import_of_part_of_a_pattern_counts_the_files_it_covers():
    templates = deleted_templates(10)
    index = make_file("src/legacy/index.ts", added=["// done"],
                      removed=[f"export * from './templates/t{number}';" for number in range(4)])
    _, _, document = finalize(templates + [index])
    (edge,) = document["edges"]
    assert edge["label"] == "imports (4)" and edge["claim"] == 'index.ts imported 4 of the 10 files in "10 files: deleted".'
