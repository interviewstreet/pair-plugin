import copy
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import paircli_architecture
from paircli_architecture import MAX_BYTES, validate_architecture, write_fallback_architecture
from paircli_skeleton import (
    BUDGET_TIERS, MAX_NODES, build_skeleton, classify, deterministic_architecture, size_budget, skeleton_stats,
)
from snapshot_factory import (
    HEAD, deleted_templates, make_file, make_hunk, make_snapshot, many_files, repeated_edits, source_lines,
    version_bumps,
)


def build(files, **kwargs):
    snapshot = make_snapshot(files, **kwargs)
    return snapshot, build_skeleton(snapshot, HEAD)


def diagram_for(files, **kwargs):
    """Build and validate the deterministic diagram; None when there is nothing to draw."""
    snapshot, skeleton = build(files, **kwargs)
    document = deterministic_architecture(skeleton, HEAD, snapshot)
    if document is not None:
        validate_architecture(document, HEAD, snapshot)
    return document, skeleton, snapshot


def edges_of(skeleton, side="new"):
    return [(e["source_file"], e["target_file"], e["cite"]["line"])
            for e in skeleton["edges"] if e["cite"]["side"] == side]


def record(skeleton, path):
    return next(item for item in skeleton["files"] if item["path"] == path)


# ---- budget ------------------------------------------------------------------------------------

@pytest.mark.parametrize("files,groups,rows,edges", [
    (0, 1, 12, 0), (1, 1, 12, 0), (2, 3, 12, 6), (3, 3, 12, 6), (4, 5, 12, 10), (12, 8, 12, 16),
    (21, 12, 10, 24), (34, 12, 10, 24), (51, 16, 8, 30), (121, 20, 8, 30), (253, 20, 8, 30), (1000, 20, 8, 30),
])
def test_size_budget_table_boundaries(files, groups, rows, edges):
    assert size_budget(files, 0) == {"max_groups": groups, "max_rows_per_group": rows, "max_edges": edges}


def test_size_budget_moves_up_a_tier_for_large_changes_and_never_exceeds_v1_caps():
    assert size_budget(3, 5000)["max_groups"] == 5
    assert size_budget(1000, 10**6)["max_groups"] == 20
    for files in (0, 1, 7, 100, 10**5):
        for lines in (0, 10**7):
            budget = size_budget(files, lines)
            assert 1 <= budget["max_groups"] <= MAX_NODES and budget["max_edges"] <= 30
            assert budget["max_rows_per_group"] <= 12
    assert size_budget("x", None) == size_budget(0, 0)
    assert [tier[1] for tier in BUDGET_TIERS] == sorted(tier[1] for tier in BUDGET_TIERS)


# ---- classification -----------------------------------------------------------------------------

@pytest.mark.parametrize("path,language,category", [
    ("src/app.tsx", "tsx", "source"), ("src/app.test.ts", "ts", "test"), ("tests/test_x.py", "py", "test"),
    ("docs/guide.md", "md", "docs"), ("README", "other", "docs"), ("package-lock.json", "json", "lockfile"),
    ("go.sum", "other", "lockfile"), ("web/dist/app.js", "js", "generated"), ("web/app.min.js", "js", "generated"),
    ("a/__snapshots__/x.snap", "other", "generated"), ("vendor/lib/x.go", "go", "vendor"),
    ("api/x_pb2.py", "py", "generated"), ("logo.png", "other", "binary"), ("tsconfig.json", "json", "config"),
    (".github/workflows/ci.yml", "yaml", "config"), ("requirements.txt", "other", "config"),
    ("db/001.sql", "sql", "source"), ("run.sh", "sh", "source"), ("lib/x.go", "go", "source"),
])
def test_classify(path, language, category):
    assert classify(path) == (language, category)


# ---- import extraction and resolution --------------------------------------------------------

def test_typescript_imports_resolve_with_exact_citation_lines():
    main = [
        "import { a } from './a';",                  # 2
        "import b from './b';",                      # 3: directory index
        "export * from '../shared/c';",              # 4
        "const d = require('./d.js');",              # 5: .js specifier, .ts file
        "const e = await import('./e');",            # 6
        "import {",                                  # 7
        "  x,",                                      # 8
        "} from './f';",                             # 9: multi-line import cites the from line
        "import React from 'react';",                # 10: external
        "import z from './missing';",                # 11: unresolved
        "import u from './unchanged';",              # 12: exists but is not part of this PR
        "import self from './main';",                # 13: self
        "// import c from './a';",                   # 14: comment
        "import './side.css';",                      # 15: side-effect import of a changed file
    ]
    files = [make_file("src/app/main.ts", added=main)] + [
        make_file(path, added=["export const v = 1;"]) for path in (
            "src/app/a.ts", "src/app/b/index.tsx", "src/shared/c.js", "src/app/d.ts", "src/app/e.jsx",
            "src/app/f.ts", "src/app/side.css")
    ]
    _, skeleton = build(files)
    assert edges_of(skeleton) == [
        ("src/app/main.ts", "src/app/a.ts", 2), ("src/app/main.ts", "src/app/b/index.tsx", 3),
        ("src/app/main.ts", "src/shared/c.js", 4), ("src/app/main.ts", "src/app/d.ts", 5),
        ("src/app/main.ts", "src/app/e.jsx", 6), ("src/app/main.ts", "src/app/f.ts", 9),
        ("src/app/main.ts", "src/app/side.css", 15),
    ]
    assert all(e["kind"] == "import" and e["cite"]["path"] == "src/app/main.ts" for e in skeleton["edges"])


def test_typescript_alias_imports_match_changed_files_by_suffix():
    files = [make_file("app/page.tsx", added=["import { api } from '@/lib/api/client';"]),
             make_file("app/lib/api/client.ts", added=["export const api = 1;"]),
             make_file("other/client.ts", added=["x"])]
    _, skeleton = build(files)
    assert edges_of(skeleton) == [("app/page.tsx", "app/lib/api/client.ts", 2)]


def test_a_new_side_import_of_a_deleted_file_is_not_an_edge():
    files = [make_file("src/a.ts", added=["import { b } from './b';"]),
             make_file("src/b.ts", status="deleted", removed=["export const b = 1;"])]
    _, skeleton = build(files)
    assert skeleton["edges"] == []


def test_removed_imports_are_old_side_edges_and_stay_out_of_the_diagram():
    changed = make_hunk(1, added=["import { c } from '../api/c';"], removed=["import { b } from '../api/b';"])
    files = [make_file("web/a.ts", hunks=[changed]), make_file("api/b.ts", added=["x"]), make_file("api/c.ts", added=["x"])]
    document, skeleton, _ = diagram_for(files)
    assert edges_of(skeleton, "old") == [("web/a.ts", "api/b.ts", 2)]
    assert edges_of(skeleton, "new") == [("web/a.ts", "api/c.ts", 2)]
    old = next(e for e in skeleton["edges"] if e["cite"]["side"] == "old")
    assert old["source_group"] == "web" and old["target_group"] == "api"
    assert [(e["source"], e["target"]) for e in document["edges"]] == [("web", "api")]
    assert document["edges"][0]["description"] == "a.ts imports c.ts"


def test_python_imports_resolve_dotted_and_relative_names():
    main = [
        "import pkg.util.helpers",                   # 2
        "from pkg.models import User, Order",        # 3: the package module
        "from . import sibling",                     # 4
        "from .services import run",                 # 5: package __init__
        "from .. import core",                       # 6
        "import os, sys",                            # 7
        "from pkg.plugins import loader as ld",      # 8: a submodule imported by name
        "from hooks.snap import grab",               # 9: suffix match under another root
        "# from . import ignored",                   # 10
    ]
    paths = ["pkg/util/helpers.py", "pkg/models/__init__.py", "pkg/app/sibling.py", "pkg/app/services/__init__.py",
             "pkg/core.py", "pkg/plugins/loader.py", "tools/hooks/snap.py", "pkg/app/ignored.py"]
    files = [make_file("pkg/app/main.py", added=main)] + [make_file(path, added=["x = 1"]) for path in paths]
    _, skeleton = build(files)
    assert edges_of(skeleton) == [
        ("pkg/app/main.py", "pkg/util/helpers.py", 2), ("pkg/app/main.py", "pkg/models/__init__.py", 3),
        ("pkg/app/main.py", "pkg/app/sibling.py", 4), ("pkg/app/main.py", "pkg/app/services/__init__.py", 5),
        ("pkg/app/main.py", "pkg/core.py", 6), ("pkg/app/main.py", "pkg/plugins/loader.py", 8),
        ("pkg/app/main.py", "tools/hooks/snap.py", 9),
    ]


def test_go_imports_resolve_single_block_and_aliased_lines():
    handler = [
        'import "github.com/acme/repo/internal/store"',      # 2
        "import (",                                          # 3
        '\t"fmt"',                                           # 4: standard library
        '\tlog "github.com/acme/repo/internal/log"',         # 5: alias
        '\t_ "github.com/acme/repo/internal/drivers"',       # 6: blank alias
        '\t"github.com/other/lib"',                          # 7: external
        '\t"github.com/acme/repo/internal/handler" // self', # 8: own directory
        ")",
    ]
    # Go import paths end with the repository-relative directory, so directories match by suffix.
    files = [make_file("internal/handler/handler.go", added=handler),
             make_file("internal/store/store.go", added=["package store"]),
             make_file("internal/store/store_test.go", added=source_lines(30)),
             make_file("internal/log/log.go", added=["package log"]),
             make_file("internal/drivers/pg.go", added=["package drivers"])]
    _, skeleton = build(files)
    assert edges_of(skeleton) == [
        ("internal/handler/handler.go", "internal/store/store.go", 2),
        ("internal/handler/handler.go", "internal/log/log.go", 5),
        ("internal/handler/handler.go", "internal/drivers/pg.go", 6),
    ]


def go_package_files():
    """cmd/app imports internal/scan (three changed files), internal/solo (one) and internal/llm (one + a test)."""
    main = [
        'import (',                                           # 2
        '\t"github.com/acme/repo/internal/scan"',            # 3: a package with several changed files
        '\t"github.com/acme/repo/internal/solo"',            # 4: a package with exactly one changed file
        '\t"github.com/acme/repo/internal/llm"',             # 5: one changed non-test file plus a test file
        ')',
    ]
    return [make_file("cmd/app/main.go", added=main),
            make_file("internal/scan/replay.go", added=source_lines(30)),
            make_file("internal/scan/scan.go", added=source_lines(5)),
            make_file("internal/scan/output.go", added=source_lines(3)),
            make_file("internal/solo/solo.go", added=["package solo"]),
            make_file("internal/llm/messages.go", added=["package llm"]),
            make_file("internal/llm/messages_test.go", added=source_lines(40))]


def test_a_go_import_is_a_file_edge_only_when_the_package_has_exactly_one_changed_file():
    _, skeleton = build(go_package_files())
    file_edges = [(e["target_file"], e["cite"]["line"]) for e in skeleton["edges"] if e["target_file"]]
    assert file_edges == [("internal/solo/solo.go", 4), ("internal/llm/messages.go", 5)]
    (package,) = [e for e in skeleton["edges"] if e["target_file"] is None]
    assert package["target_package"] == "internal/scan" and package["cite"]["line"] == 3
    # The package edge still carries its groups, so the v1 diagram can aggregate group to group.
    assert package["source_group"] and package["target_group"]
    assert package["target_anchor"].startswith("internal/scan/")
    # No file-level edge names any of the several scan files.
    assert not [e for e in skeleton["edges"] if (e["target_file"] or "").startswith("internal/scan/")]


def test_a_go_package_edge_feeds_the_v1_diagram_without_naming_a_file():
    files = [make_file("cmd/app/main.go", added=['import "github.com/acme/repo/internal/scan"']),
             make_file("internal/scan/replay.go", added=source_lines(30)),
             make_file("internal/scan/scan.go", added=source_lines(5))]
    document, skeleton, _ = diagram_for(files)
    assert [e["target_file"] for e in skeleton["edges"]] == [None]
    (edge,) = document["edges"]
    assert edge["description"] == "main.go imports the internal/scan package"
    assert "replay.go" not in edge["description"] and "scan.go" not in edge["description"]
    assert {node["id"] for node in document["nodes"]} >= {edge["source"], edge["target"]}


def test_a_go_import_of_a_package_whose_other_files_are_deleted_or_tests_resolves_to_the_live_file():
    files = [make_file("cmd/app/main.go", added=['import "github.com/acme/repo/internal/old"']),
             make_file("internal/old/new.go", added=["package old"]),
             make_file("internal/old/gone.go", status="deleted", removed=["package old"]),
             make_file("internal/old/new_test.go", added=["package old"])]
    _, skeleton = build(files)
    assert edges_of(skeleton) == [("cmd/app/main.go", "internal/old/new.go", 2)]


def test_ambiguous_python_and_alias_suffix_matches_give_no_edge():
    files = [make_file("pkg/app/main.py", added=["from shared.util import x", "import only.here"]),
             make_file("a/shared/util.py", added=["x = 1"]), make_file("b/shared/util.py", added=["x = 1"]),
             make_file("lib/only/here.py", added=["y = 1"]),
             make_file("app/page.tsx", added=["import { c } from '@/lib/client';", "import { d } from '@/lib/solo';"]),
             make_file("one/lib/client.ts", added=["x"]), make_file("two/lib/client.ts", added=["x"]),
             make_file("one/lib/solo.ts", added=["x"])]
    _, skeleton = build(files)
    assert edges_of(skeleton) == [("app/page.tsx", "one/lib/solo.ts", 3), ("pkg/app/main.py", "lib/only/here.py", 3)]


def test_edges_are_deduplicated_ordered_and_capped():
    lines = [f"import x{index} from './t{index % 3}';" for index in range(6)]
    files = [make_file("src/a.ts", added=lines)] + [make_file(f"src/t{index}.ts", added=["x"]) for index in range(3)]
    _, skeleton = build(files)
    assert len(skeleton["edges"]) == 6
    assert skeleton["edges"] == sorted(skeleton["edges"], key=lambda e: (e["source_file"], e["cite"]["line"]))
    files = [make_file(f"src/f{index}.ts", added=[f"import a{k} from './f{(index + k) % 2500}';" for k in range(1, 4)])
             for index in range(1000)]
    _, skeleton = build(files)
    assert len(skeleton["edges"]) == 2000


# ---- grouping, ranking and routine files -------------------------------------------------------

@pytest.mark.parametrize("count", [1, 3, 12, 34, 253, 1000])
def test_group_count_fits_the_budget_and_every_file_lands_in_one_group(count):
    files = many_files(count)
    _, skeleton = build(files)
    groups = skeleton["groups"]
    assert skeleton["budget"] == size_budget(count, count * 7)
    assert 1 <= len(groups) <= skeleton["budget"]["max_groups"]
    listed = [path for group in groups for path in group["files"]]
    assert sorted(listed) == sorted(file["path"] for file in files)
    ids = [group["id"] for group in groups]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", group_id) for group_id in ids)
    assert all(len(group["label"]) <= 80 for group in groups)
    assert [(-g["score"], g["path"]) for g in groups] == sorted((-g["score"], g["path"]) for g in groups)


def test_clustering_goes_deeper_than_the_top_level_for_big_prs_and_stays_shallow_for_small_ones():
    _, big = build(many_files(253))
    assert 2 < len(big["groups"]) <= 20
    assert max(group["path"].count("/") for group in big["groups"]) >= 1
    _, small = build([make_file("a/x.ts", added=["x"]), make_file("b/y.ts", added=["x"]), make_file("c/z.ts", added=["x"])])
    assert sorted(group["path"] for group in small["groups"]) == ["a", "b", "c"]
    _, one_dir = build([make_file(f"src/m{index}/x.ts", added=["x"]) for index in range(3)])
    assert len(one_dir["groups"]) <= 3


def test_tiny_sibling_groups_merge_into_their_parent_and_overflow_goes_to_other():
    files = [make_file(f"d{index}/x.ts", added=["x"]) for index in range(30)]
    _, skeleton = build(files)
    assert len(skeleton["groups"]) <= 12
    assert any(group["id"] == "other" and group["label"] == "Other changes" for group in skeleton["groups"])
    deep = [make_file(f"app/area{index}/leaf/x.ts", added=["x"]) for index in range(10)] + [
        make_file("app/area0/leaf/y.ts", added=["x"])]
    _, skeleton = build(deep)
    assert 1 <= len(skeleton["groups"]) <= 8


def test_routine_files_collapse_into_a_count_not_rows():
    files = [make_file("web/src/app.ts", added=source_lines(5)),
             make_file("web/package-lock.json", added=source_lines(500)),
             make_file("web/src/api.generated.ts", added=source_lines(50)),
             make_file("web/dist/bundle.js", added=source_lines(50)),
             make_file("vendor/lib.go", added=source_lines(10)),
             make_file("logo.png", no_hunks=True)]
    document, skeleton, _ = diagram_for(files)
    web = next(group for group in skeleton["groups"] if group["path"] == "web")
    assert web["files"] == ["web/src/app.ts"] and web["routine_count"] == 3
    assert skeleton["totals"]["routine_files"] == 5 and skeleton["routine_unassigned"] == 2
    assert all(record(skeleton, path)["routine"] for path in ("web/package-lock.json", "vendor/lib.go", "logo.png"))
    assert [node["files"] for node in document["nodes"]] == [["web/src/app.ts"]]
    assert "3 routine files collapsed" in document["nodes"][0]["description"]
    assert any("5 routine files" in gap for gap in document["gaps"])
    assert skeleton_stats(skeleton)["routine_files"] == 5


def test_sensitive_paths_outrank_bigger_plain_changes_and_tests_rank_below_source():
    files = [make_file("auth/session.ts", added=source_lines(1)),
             make_file("billing/invoice.ts", added=source_lines(60)),
             make_file("ui/button.test.ts", added=source_lines(60)),
             make_file("ui/button.ts", added=source_lines(60)),
             make_file("migrations/001.sql", added=source_lines(2))]
    _, skeleton = build(files)
    order = [group["path"] for group in skeleton["groups"]]
    assert order.index("auth") < order.index("billing") and order.index("migrations") < order.index("billing")
    assert record(skeleton, "auth/session.ts")["sensitive"] and record(skeleton, "migrations/001.sql")["sensitive"]
    assert record(skeleton, "auth/session.ts")["score"] > record(skeleton, "billing/invoice.ts")["score"]
    assert record(skeleton, "ui/button.ts")["score"] > record(skeleton, "ui/button.test.ts")["score"]
    ui = next(group for group in skeleton["groups"] if group["path"] == "ui")
    assert ui["files"] == ["ui/button.ts", "ui/button.test.ts"]
    for word in ("Auth", "SECRET", "token", "credential", "password", "migration", "schema", "permission",
                 ".env", "workflow", "Dockerfile", "security"):
        assert build([make_file(f"a/{word}/x.ts", added=["x"])])[1]["files"][0]["sensitive"], word
    assert not build([make_file("a/plain/x.ts", added=["x"])])[1]["files"][0]["sensitive"]


def test_group_totals_change_and_sensitivity():
    files = [make_file("lib/new1.ts", status="added", added=source_lines(3)),
             make_file("lib/new2.ts", status="added", added=source_lines(2)),
             make_file("old/gone.ts", status="deleted", removed=source_lines(4)),
             make_file("mix/a.ts", status="added", added=["x"]), make_file("mix/b.ts", removed=["y"], added=["z"])]
    _, skeleton = build(files)
    by_path = {group["path"]: group for group in skeleton["groups"]}
    assert by_path["lib"]["change"] == "added" and (by_path["lib"]["additions"], by_path["lib"]["deletions"]) == (5, 0)
    assert by_path["old"]["change"] == "removed" and by_path["old"]["deletions"] == 4
    assert by_path["mix"]["change"] == "modified"
    assert record(skeleton, "old/gone.ts")["has_new_lines"] is False
    assert record(skeleton, "lib/new1.ts")["additions"] == 3


# ---- renames, deletions, hunk-less files --------------------------------------------------------

def test_renames_use_the_new_path_and_record_the_old_one():
    files = [make_file("src/new_name.ts", status="renamed", old_path="src/old_name.ts", added=["export const a = 1;"]),
             make_file("src/moved_only.ts", status="renamed", old_path="lib/moved_only.ts", no_hunks=True)]
    document, skeleton, _ = diagram_for(files)
    assert (record(skeleton, "src/new_name.ts")["old_path"], record(skeleton, "src/new_name.ts")["status"]) == (
        "src/old_name.ts", "renamed")
    assert record(skeleton, "src/moved_only.ts")["lines_available"] is False
    assert document["nodes"][0]["files"] == ["src/new_name.ts"]
    assert all("old_name" not in item["path"] for node in document["nodes"] for item in node["evidence"])
    assert any("no line data" in gap for gap in document["gaps"])


def test_deleted_only_groups_are_omitted_with_a_gap_and_mixed_groups_still_work():
    files = [make_file("gone/a.ts", status="deleted", removed=source_lines(5)),
             make_file("gone/b.ts", status="deleted", removed=source_lines(5)),
             make_file("mixed/keep.ts", added=source_lines(4)),
             make_file("mixed/drop.ts", status="deleted", removed=source_lines(9))]
    document, skeleton, _ = diagram_for(files)
    assert [node["id"] for node in document["nodes"]] == ["mixed"]
    assert document["nodes"][0]["files"] == ["mixed/keep.ts"]
    assert "2 files changed (+4/-9)" in document["nodes"][0]["description"]
    assert any("no new lines to cite" in gap for gap in document["gaps"])
    assert document["description"] == "Changes 4 files in 2 areas (+4/-19)."
    assert {group["path"] for group in skeleton["groups"]} == {"gone", "mixed"}


def test_a_pr_that_only_deletes_has_no_diagram():
    files = [make_file("gone/a.ts", status="deleted", removed=source_lines(5))]
    snapshot, skeleton = build(files)
    assert skeleton["no_citable_lines"] is True and skeleton["docs_only"] is False
    assert deterministic_architecture(skeleton, HEAD, snapshot) is None


def test_hunkless_files_are_listed_but_never_cited():
    files = [make_file("src/with.ts", added=["x = 1;"]), make_file("src/without.ts", no_hunks=True),
             make_file("src/missing_key.ts")]
    del files[2]["hunks"]
    document, skeleton, _ = diagram_for(files)
    assert [record(skeleton, path)["lines_available"] for path in ("src/with.ts", "src/without.ts", "src/missing_key.ts")] == [
        True, False, False]
    assert document["nodes"][0]["files"] == ["src/with.ts"]
    assert any("2 files had no line data" in gap for gap in document["gaps"])
    only_hunkless = [make_file("src/without.ts", no_hunks=True)]
    snapshot, skeleton = build(only_hunkless)
    assert skeleton["no_citable_lines"] and deterministic_architecture(skeleton, HEAD, snapshot) is None


def test_docs_only_prs_have_no_diagram():
    for files in ([make_file("README.md", added=["hello"]), make_file("docs/guide.rst", added=["x"]),
                   make_file("docs/img.png", no_hunks=True)],
                  [make_file("CHANGELOG.txt", added=["x"]), make_file("yarn.lock", added=["x"])], []):
        snapshot, skeleton = build(files)
        assert skeleton["docs_only"] is True
        assert deterministic_architecture(skeleton, HEAD, snapshot) is None
    _, skeleton = build([make_file("yarn.lock", added=["x"])])
    assert skeleton["docs_only"] is False and skeleton["groups"] == []
    assert deterministic_architecture(skeleton, HEAD, make_snapshot([make_file("yarn.lock", added=["x"])])) is None
    _, skeleton = build([make_file("README.md", added=["x"]), make_file("src/a.ts", added=["x"])])
    assert skeleton["docs_only"] is False


# ---- diagram shape ------------------------------------------------------------------------------

def test_a_single_file_pr_gives_a_valid_one_node_diagram():
    document, skeleton, _ = diagram_for([make_file("src/only.ts", added=source_lines(3))], title="Add only")
    assert document["title"] == "Add only" and document["description"] == "Changes 1 file in 1 area (+3/-0)."
    assert document["schema_version"] == 1 and document["head_sha"] == HEAD
    assert document["edges"] == [] and document["flows"] == []
    [node] = document["nodes"]
    assert node["kind"] == "service" and node["layer"] == 0 and node["change"] == "modified"
    assert node["description"] == "1 file changed (+3/-0): only.ts."
    assert node["evidence"] == [{"type": "diff", "path": "src/only.ts", "start": 2, "end": 4}]
    assert document["gaps"] == ["Generated deterministically from file structure and imports, not from reading the code."]


def test_node_kinds_follow_path_words():
    files = [make_file("migrations/001.ts", added=["x"]), make_file("components/a.tsx", added=["x"]),
             make_file("workers/send.py", added=["x"]), make_file("api/handler.ts", added=["x"]),
             make_file("sql/q.sql", added=["x"])]
    document, _, _ = diagram_for(files)
    kinds = {node["label"]: node["kind"] for node in document["nodes"]}
    assert kinds == {"migrations": "database", "components": "client", "workers": "worker", "api": "service",
                     "sql": "database"}


def test_evidence_covers_every_listed_file_and_prefers_the_biggest_hunks():
    small, big = make_hunk(1, added=["a"]), make_hunk(50, added=source_lines(30))
    files = [make_file("src/two.ts", hunks=[small, big])] + [make_file(f"src/f{index}.ts", added=source_lines(2)) for index in range(13)]
    document, _, _ = diagram_for(files)
    [node] = document["nodes"]
    evidence = node["evidence"]
    assert len(node["files"]) <= 12 and len(evidence) <= 12
    assert {item["path"] for item in evidence} >= set(node["files"])
    first = next(item for item in evidence if item["path"] == "src/two.ts")
    assert (first["start"], first["end"]) == (51, 80)  # the 30 added lines of the bigger hunk
    spare = [make_file("src/solo.ts", hunks=[make_hunk(1 + 10 * k, added=["x"]) for k in range(20)])]
    document, _, _ = diagram_for(spare)
    assert len(document["nodes"][0]["evidence"]) == 12


def test_layers_follow_the_group_import_dag_and_break_cycles_deterministically():
    files = [make_file("a/x.ts", added=["import b from '../b/x';"]), make_file("b/x.ts", added=["import c from '../c/x';"]),
             make_file("c/x.ts", added=["export const c = 1;"]), make_file("d/x.ts", added=["export const d = 1;"])]
    document, _, _ = diagram_for(files)
    layers = {node["id"]: node["layer"] for node in document["nodes"]}
    assert layers == {"a": 0, "b": 1, "c": 2, "d": 0}
    cyclic = [make_file("a/x.ts", added=["import b from '../b/x';"]), make_file("b/x.ts", added=["import a from '../a/x';"])]
    first, _, _ = diagram_for(cyclic)
    second, _, _ = diagram_for(cyclic)
    assert first == second and sorted(node["layer"] for node in first["nodes"]) == [0, 1]
    chain = [make_file(f"g{index:02d}/x.ts", added=[f"import n from '../g{index + 1:02d}/x';"]) for index in range(11)]
    chain.append(make_file("g11/x.ts", added=["x"]))
    chain += [make_file(f"g00/extra{index}.ts", added=["x"]) for index in range(13)]  # 25 files: up to 12 groups
    document, _, _ = diagram_for(chain)
    assert max(node["layer"] for node in document["nodes"]) == 8


def test_group_edges_aggregate_imports_and_keep_the_strongest():
    files = [make_file("web/a.ts", added=["import x from '../api/x';", "import y from '../api/y';"]),
             make_file("web/b.ts", added=["import x from '../api/x';"]),
             make_file("api/x.ts", added=["x"]), make_file("api/y.ts", added=["x"]),
             make_file("db/z.ts", added=["x"]), make_file("web/c.ts", added=["import z from '../db/z';"])]
    document, skeleton, _ = diagram_for(files)
    edges = {(edge["source"], edge["target"]): edge for edge in document["edges"]}
    assert set(edges) == {("web", "api"), ("web", "db")}
    heavy = edges[("web", "api")]
    assert heavy["label"] == "imports" and heavy["change"] == "modified" and heavy["id"] != edges[("web", "db")]["id"]
    assert [(item["path"], item["start"], item["end"]) for item in heavy["evidence"]] == [
        ("web/a.ts", 2, 2), ("web/a.ts", 3, 3), ("web/b.ts", 2, 2)]
    assert heavy["description"] == "a.ts imports x.ts and 2 more imports"
    # Cap: only the strongest edges survive a small edge budget.
    snapshot = make_snapshot(files)
    skeleton["budget"] = dict(skeleton["budget"], max_edges=1)
    trimmed = deterministic_architecture(skeleton, HEAD, snapshot)
    assert [(e["source"], e["target"]) for e in trimmed["edges"]] == [("web", "api")]
    validate_architecture(trimmed, HEAD, snapshot)


def test_added_groups_with_an_import_between_them_make_an_added_edge():
    files = [make_file("web/a.ts", status="added", added=["import x from '../api/x';"]),
             make_file("api/x.ts", status="added", added=["x"])]
    document, _, _ = diagram_for(files)
    assert [node["change"] for node in document["nodes"]] == ["added", "added"]
    assert document["edges"][0]["change"] == "added"


def test_extra_groups_beyond_the_node_budget_merge_into_other_changes():
    files = [make_file(f"area{index}/x.ts", added=[f"import n from '../area{(index + 1) % 5}/x';"] + source_lines(index + 1))
             for index in range(5)]
    snapshot, skeleton = build(files)
    skeleton["budget"] = dict(skeleton["budget"], max_groups=3)
    document = deterministic_architecture(skeleton, HEAD, snapshot)
    validate_architecture(document, HEAD, snapshot)
    assert len(document["nodes"]) == 3
    other = document["nodes"][-1]
    assert other["label"] == "Other changes" and other["id"] == "other" and len(other["files"]) >= 2
    assert any("merged into Other changes" in gap for gap in document["gaps"])
    assert {edge["source"] for edge in document["edges"]} <= {node["id"] for node in document["nodes"]}


def test_title_description_and_gaps_obey_the_contract_limits():
    files = [make_file("a/x.ts", added=["x"]), make_file("yarn.lock", added=["x"]), make_file("p.png", no_hunks=True),
             make_file("b/y.ts", no_hunks=True), make_file("gone/z.ts", status="deleted", removed=["x"])]
    document, _, _ = diagram_for(files, title="  " + "very long title\n" * 40)
    assert 0 < len(document["title"]) <= 160 and "\n" not in document["title"]
    assert len(document["gaps"]) <= 5 and all(0 < len(gap) <= 300 for gap in document["gaps"])
    assert document["gaps"][-1].startswith("Generated deterministically from file structure and imports")
    document, _, _ = diagram_for([make_file("a/x.ts", added=["x"])], title="")
    assert document["title"] == "Pull request changes"


@pytest.mark.parametrize("path", [
    "docs/My Folder/ñandú/notes [draft] *x* `y`.ts", "src/日本語/ファイル.ts", "src/with space/a b.ts",
    "src/_underscore/-dash/1numeric/x.ts", "src/a‮b/x.ts", "a/" + "d" * 80 + "/x.ts",
])
def test_odd_paths_produce_valid_diagrams(path):
    document, skeleton, _ = diagram_for([make_file(path, added=["x"]), make_file("src/plain.ts", added=["x"])])
    assert document["nodes"] and all(len(node["label"]) <= 80 for node in document["nodes"])
    assert path in [item["path"] for node in document["nodes"] for item in node["evidence"]]
    json.dumps(skeleton)


def test_overlong_paths_are_never_cited():
    long_path = "src/" + "x" * 320 + ".ts"
    document, _, _ = diagram_for([make_file(long_path, added=["x"]), make_file("src/ok.ts", added=["x"])])
    assert document["nodes"][0]["files"] == ["src/ok.ts"]


def test_the_skeleton_is_defensive_about_odd_shapes():
    weird = [None, {}, {"pr": None}, {"pr": {"files": None}}, {"pr": {"files": [None, 3, "x", {"path": 7}, {"path": ""}]}},
             {"pr": {"files": [{"path": "a.ts", "hunks": "bad"}, {"path": "b.ts", "hunks": [None, 3, {"lines": "x"}]},
                               {"path": "c.ts", "hunks": [{"new_start": "1", "new_lines": 2.5, "lines": [None, {"kind": "??"},
                                                                                              {"kind": "add", "text": 7}]}]},
                               {"path": "d.ts", "status": 5, "old_path": 3, "hunks": [{"new_start": 2, "new_lines": 1,
                                                                                       "lines": [{"kind": "add", "text": "import x from './a';"}]}]}]}}]
    for snapshot in weird:
        skeleton = build_skeleton(snapshot, HEAD)
        json.dumps(skeleton)
        deterministic_architecture(skeleton, HEAD, snapshot if isinstance(snapshot, dict) else {})
    skeleton = build_skeleton(weird[-1], HEAD)
    assert [item["path"] for item in skeleton["files"]] == ["a.ts", "b.ts", "c.ts", "d.ts"]
    assert record(skeleton, "d.ts")["status"] == "modified" and record(skeleton, "d.ts")["old_path"] is None
    assert build_skeleton({"pr": {"files": []}}, None)["head"] == ""


def test_kind_values_and_numbers_are_read_from_real_hunk_lines():
    hunk = {"old_start": 10, "old_lines": 2, "new_start": 10, "new_lines": 2, "section": "", "lines": [
        {"kind": "ctx", "text": "a", "old_no": 10, "new_no": 10},
        {"kind": "del", "text": "import o from './old';", "old_no": 11},
        {"kind": "add", "text": "import n from './new';", "new_no": 11}]}
    files = [make_file("src/a.ts", hunks=[hunk]), make_file("src/old.ts", added=["x"]), make_file("src/new.ts", added=["x"])]
    _, skeleton = build(files)
    assert record(skeleton, "src/a.ts")["additions"] == 1 and record(skeleton, "src/a.ts")["deletions"] == 1
    assert edges_of(skeleton, "old") == [("src/a.ts", "src/old.ts", 11)]
    assert edges_of(skeleton, "new") == [("src/a.ts", "src/new.ts", 11)]
    # Missing per-line numbers are rebuilt from the hunk header.
    bare = copy.deepcopy(hunk)
    for line in bare["lines"]:
        line.pop("old_no", None)
        line.pop("new_no", None)
    _, skeleton = build([make_file("src/a.ts", hunks=[bare]), make_file("src/old.ts", added=["x"]), make_file("src/new.ts", added=["x"])])
    assert edges_of(skeleton, "old") == [("src/a.ts", "src/old.ts", 11)]
    assert edges_of(skeleton, "new") == [("src/a.ts", "src/new.ts", 11)]


def test_counts_fall_back_to_file_totals_when_hunks_carry_no_lines():
    entry = {"path": "src/a.ts", "status": "modified", "additions": 5, "deletions": 2,
             "hunks": [{"new_start": 10, "new_lines": 5}]}
    document, skeleton, _ = diagram_for([entry])
    assert (record(skeleton, "src/a.ts")["additions"], record(skeleton, "src/a.ts")["deletions"]) == (5, 2)
    assert document["nodes"][0]["evidence"] == [{"type": "diff", "path": "src/a.ts", "start": 10, "end": 14}]


# ---- determinism, scale ---------------------------------------------------------------------------

def test_the_same_snapshot_gives_byte_identical_output():
    files = many_files(120) + [make_file("web/a.ts", added=["import x from '../api/x';"]), make_file("api/x.ts", added=["x"])]
    outputs = []
    for _ in range(2):
        snapshot = make_snapshot(copy.deepcopy(files))
        skeleton = build_skeleton(snapshot, HEAD)
        outputs.append((json.dumps(skeleton), json.dumps(deterministic_architecture(skeleton, HEAD, snapshot))))
    assert outputs[0] == outputs[1]
    reordered = make_snapshot(list(reversed(files)))
    skeleton = build_skeleton(reordered, HEAD)
    assert json.dumps(skeleton["groups"]) == json.dumps(json.loads(outputs[0][0])["groups"])


@pytest.mark.parametrize("count", [1, 3, 12, 34, 253])
def test_synthetic_prs_of_every_size_validate(count):
    files = many_files(count) + [make_file("package-lock.json", added=source_lines(10))]
    document, skeleton, _ = diagram_for(files)
    assert document["nodes"] and len(document["nodes"]) <= skeleton["budget"]["max_groups"]
    assert len(document["edges"]) <= skeleton["budget"]["max_edges"]


def test_a_thousand_files_finish_quickly_and_stay_within_the_size_limit():
    files = many_files(1500, lines=20)
    for index in range(0, 1500, 3):
        files[index]["hunks"][0]["lines"].insert(1, {"kind": "add", "text": f"import x from './file{index + 1}';", "new_no": 2})
    snapshot = make_snapshot(files)
    started = time.perf_counter()
    skeleton = build_skeleton(snapshot, HEAD)
    document = deterministic_architecture(skeleton, HEAD, snapshot)
    elapsed = time.perf_counter() - started
    validate_architecture(document, HEAD, snapshot)
    assert elapsed < 2.0, elapsed
    assert len(json.dumps(document, ensure_ascii=False).encode()) < MAX_BYTES
    assert len(document["nodes"]) <= 20 and len(document["edges"]) <= 30
    assert skeleton["totals"]["files"] == 1500


# ---- writing the fallback -------------------------------------------------------------------------

def artifact_folder(tmp_path, snapshot):
    folder = tmp_path / ".paircli" / "pr-7" / HEAD
    folder.mkdir(parents=True)
    (folder / "snapshot.json").write_text(json.dumps(snapshot))
    return folder


def status_of(folder):
    return json.loads((folder / "diagnostics" / "architecture-status.json").read_text())


def test_write_fallback_architecture_saves_a_validated_diagram_and_the_status(tmp_path):
    snapshot = make_snapshot([make_file("web/a.ts", added=["import x from '../api/x';"]), make_file("api/x.ts", added=["x"]),
                              make_file("yarn.lock", added=["x"])])
    folder = artifact_folder(tmp_path, snapshot)
    log = io.StringIO()
    path = write_fallback_architecture(folder, "llm unavailable", log)
    assert path == folder / "generated_artifacts" / "architecture.json"
    document = json.loads(path.read_text())
    validate_architecture(document, HEAD, snapshot)
    assert status_of(folder) == {"tier": "deterministic", "reason": "llm unavailable", "generated_by": "deterministic",
                                 "skeleton": {"files": 3, "groups": 2, "edges": 1, "routine_files": 1}}
    assert "wrote deterministic generated_artifacts/architecture.json" in log.getvalue()


def test_write_fallback_architecture_skips_docs_only_and_removes_a_stale_diagram(tmp_path):
    folder = artifact_folder(tmp_path, make_snapshot([make_file("README.md", added=["hello"])]))
    (folder / "generated_artifacts").mkdir()
    (folder / "generated_artifacts" / "architecture.json").write_text("{}")
    assert write_fallback_architecture(folder, "llm unavailable", io.StringIO()) is None
    assert not (folder / "generated_artifacts" / "architecture.json").exists()
    assert status_of(folder) == {"tier": "none", "reason": "docs-only change", "generated_by": None,
                                 "skeleton": {"files": 1, "groups": 1, "edges": 0, "routine_files": 0}}


def test_write_fallback_architecture_reports_no_citable_lines(tmp_path):
    folder = artifact_folder(tmp_path, make_snapshot([make_file("src/a.ts", status="deleted", removed=["x"])]))
    assert write_fallback_architecture(folder, "llm unavailable", io.StringIO()) is None
    assert status_of(folder)["reason"] == "no citable lines"


@pytest.mark.parametrize("failure", ["builder", "skeleton", "invalid", "oversized"])
def test_write_fallback_architecture_never_raises(tmp_path, monkeypatch, failure):
    snapshot = make_snapshot([make_file("src/a.ts", added=["x"])])
    folder = artifact_folder(tmp_path, snapshot)
    (folder / "generated_artifacts").mkdir()
    (folder / "generated_artifacts" / "architecture.json").write_text("{}")
    if failure == "builder":
        monkeypatch.setattr(paircli_architecture, "deterministic_architecture", lambda *_: 1 / 0)
    if failure == "skeleton":
        monkeypatch.setattr(paircli_architecture, "build_skeleton", lambda *_: (_ for _ in ()).throw(RuntimeError("x")))
    if failure == "invalid":
        monkeypatch.setattr(paircli_architecture, "deterministic_architecture",
                            lambda skeleton, head, snap: {"schema_version": 1})
    if failure == "oversized":
        real = paircli_architecture.deterministic_architecture
        monkeypatch.setattr(paircli_architecture, "deterministic_architecture",
                            lambda *args: dict(real(*args), description="x" * 1000, gaps=["y"] * 1))
        monkeypatch.setattr(paircli_architecture, "MAX_BYTES", 10)
    log = io.StringIO()
    assert write_fallback_architecture(folder, "llm unavailable", log) is None
    expected = {"builder": "ZeroDivisionError", "skeleton": "RuntimeError", "invalid": "ArchitectureError",
                "oversized": "ValueError"}[failure]
    assert status_of(folder)["tier"] == "none" and expected in status_of(folder)["reason"]
    assert expected in log.getvalue()
    assert not (folder / "generated_artifacts" / "architecture.json").exists()


def test_write_fallback_architecture_refuses_symlinked_output(tmp_path):
    snapshot = make_snapshot([make_file("src/a.ts", added=["x"])])
    folder = artifact_folder(tmp_path, snapshot)
    outside = tmp_path / "outside"
    outside.mkdir()
    (folder / "generated_artifacts").symlink_to(outside, target_is_directory=True)
    assert write_fallback_architecture(folder, "llm unavailable", io.StringIO()) is None
    assert list(outside.iterdir()) == []
    assert status_of(folder)["tier"] == "none"
    (folder / "generated_artifacts").unlink()
    (folder / "generated_artifacts").mkdir()
    target = outside / "target.json"
    (folder / "generated_artifacts" / "architecture.json").symlink_to(target)
    write_fallback_architecture(folder, "llm unavailable", io.StringIO())
    assert not target.exists()


# --- patterns --------------------------------------------------------------------------------

def group_patterns(skeleton):
    return [(group["path"], pattern) for group in skeleton["groups"] for pattern in group["patterns"]]


def test_same_change_files_form_a_pattern_and_are_marked_in_pattern():
    files = deleted_templates(6) + [make_file("src/legacy/keep.ts", added=source_lines(3))]
    _, skeleton = build(files)
    ((path, pattern),) = group_patterns(skeleton)
    assert path == "src/legacy" or path.startswith("src/legacy")
    assert pattern["family"] == "same-change" and pattern["count"] == 6 == len(pattern["members"])
    assert pattern["status"] == "deleted" and pattern["additions"] == 0 and pattern["deletions"] == 24
    assert isinstance(pattern["signature"], str) and len(pattern["signature"]) == 16
    assert pattern["members"] == sorted(pattern["members"])
    flags = {item["path"]: item["in_pattern"] for item in skeleton["files"]}
    assert [name for name, flag in flags.items() if flag] == pattern["members"]
    assert flags["src/legacy/keep.ts"] is False


def test_fewer_than_three_files_make_no_pattern():
    _, skeleton = build(deleted_templates(2))
    assert group_patterns(skeleton) == []
    assert not any(item["in_pattern"] for item in skeleton["files"])


def test_each_pattern_lives_in_one_group_and_members_are_unique():
    files = repeated_edits(48, variants=2, directory="src/modules")
    _, skeleton = build(files)
    assert len(skeleton["groups"]) > 1
    seen = set()
    for group in skeleton["groups"]:
        for pattern in group["patterns"]:
            assert not set(pattern["members"]) & seen
            seen |= set(pattern["members"])
    assert seen == {item["path"] for item in skeleton["files"] if item["in_pattern"]}
    assert sorted(pattern["count"] for _, pattern in group_patterns(skeleton)) == [24, 24], "PR-wide, one per kind"


def test_same_change_clusters_are_pr_wide_and_assigned_to_the_majority_group():
    files = ([make_file(f"src/a/m{index}.ts", start=2, removed=["const mode = 'old';"],
                        added=[f"const mode = 'new{index}';"]) for index in range(4)]
             + [make_file(f"src/b/m{index}.ts", start=2, removed=["const mode = 'old';"],
                          added=[f"const mode = 'new{index}';"]) for index in range(2)]
             + [make_file("src/b/other.ts", added=["const lonely = 1;"])])
    _, skeleton = build(files)
    ((path, pattern),) = group_patterns(skeleton)
    assert path == "src/a" and pattern["count"] == 6
    group_b = next(group for group in skeleton["groups"] if group["path"] == "src/b")
    assert group_b["patterns"] == [] and "src/b/m0.ts" in group_b["files"]
    assert all(item["in_pattern"] for item in skeleton["files"] if item["path"] != "src/b/other.ts")


def test_the_v1_diagram_is_byte_identical_with_cross_group_patterns():
    files = (version_bumps(40) + repeated_edits(120, variants=3) + deleted_templates(6)
             + [make_file("src/app/main.ts", added=["import { log } from '../modules/area0/mod0';", "const main = 1;"]),
                make_file("src/app/util.ts", added=source_lines(4))])
    snapshot, skeleton = build(files)
    document = deterministic_architecture(skeleton, HEAD, snapshot)
    # Golden value computed with the skeleton before cross-group patterns existed (same-change
    # clusters inside one directory group only): v1 reads group files, never patterns.
    digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    assert digest == "ad0129a9a1825557d6c7974b7245d21288a2a99a0be85a8b93f15eae6718f61f"
    assert any(group["patterns"] for group in skeleton["groups"])


def test_tests_docs_and_config_fold_when_a_group_has_three_and_skip_same_change_members():
    files = ([make_file(f"src/app/t{index}.test.ts", added=[f"it('case {chr(97 + index)}', () => {{}});"]) for index in range(3)]
             + [make_file(f"src/app/c{index}.json", added=[f'{{"k{chr(97 + index)}": 1}}']) for index in range(2)]
             + [make_file(f"src/app/d{index}.md", added=[f"# {chr(97 + index)}"]) for index in range(3)]
             + [make_file(f"src/app/same{index}.test.ts", added=[f"same{index}();"]) for index in range(3)])
    _, skeleton = build(files)
    found = {pattern["family"]: pattern for _, pattern in group_patterns(skeleton)}
    assert set(found) == {"same-change", "tests", "docs"}, "two config files are not enough"
    assert found["same-change"]["count"] == 3 and found["tests"]["count"] == 3 and found["docs"]["count"] == 3
    assert found["tests"]["signature"] is None and not set(found["tests"]["members"]) & set(found["same-change"]["members"])
    assert found["docs"]["status"] == "modified"


def test_hunkless_and_routine_files_are_never_pattern_members():
    files = ([make_file(f"src/x/n{index}.ts", no_hunks=True) for index in range(4)]
             + [make_file(f"src/x/dist/g{index}.js", added=["// same"]) for index in range(4)]
             + [make_file(f"src/x/k{index}.ts", added=[f"const q = {index};"]) for index in range(3)])
    _, skeleton = build(files)
    ((_, pattern),) = group_patterns(skeleton)
    assert pattern["members"] == [f"src/x/k{index}.ts" for index in range(3)]


def test_v1_diagram_is_unchanged_by_patterns():
    files = deleted_templates(6) + [make_file("src/legacy/keep.ts", added=source_lines(3))]
    snapshot, skeleton = build(files)
    document = deterministic_architecture(skeleton, HEAD, snapshot)
    assert document is None or all(node["kind"] != "pattern" for node in document["nodes"])
    stripped = copy.deepcopy(skeleton)
    for group in stripped["groups"]:
        group.pop("patterns")
    for item in stripped["files"]:
        item.pop("in_pattern")
    assert deterministic_architecture(stripped, HEAD, snapshot) == document


def test_patterns_are_deterministic_and_fast_for_a_thousand_files():
    files = repeated_edits(1000, variants=3)
    started = time.perf_counter()
    first = build(files)[1]
    assert time.perf_counter() - started < 1.0
    assert json.dumps(first, sort_keys=True) == json.dumps(build(files)[1], sort_keys=True)


def test_pure_moves_form_a_moved_pattern_assigned_to_the_group_holding_most_of_them():
    moves = [make_file(f"src/new/m{index}.ts", "renamed", old_path=f"src/old/m{index}.ts", no_hunks=True) for index in range(5)]
    files = moves + [make_file("web/own.ts", added=source_lines(3))]
    _, skeleton = build(files)
    ((path, pattern),) = group_patterns(skeleton)
    assert path == "src" and pattern["family"] == "moved" and pattern["signature"] is None
    assert pattern["status"] == "renamed" and pattern["count"] == 5 == len(pattern["members"])
    assert (pattern["additions"], pattern["deletions"]) == (0, 0) and pattern["members"] == sorted(pattern["members"])
    assert {item["path"] for item in skeleton["files"] if item["in_pattern"]} == set(pattern["members"])


def test_fewer_than_three_pure_moves_and_edited_renames_make_no_moved_pattern():
    files = [make_file(f"src/new/m{index}.ts", "renamed", old_path=f"src/old/m{index}.ts", no_hunks=True) for index in range(2)]
    files += [make_file(f"src/new/e{index}.ts", "renamed", old_path=f"src/old/e{index}.ts", added=[f"const e{'xyz'[index]} = 1;"]) for index in range(3)]
    _, skeleton = build(files)
    assert [pattern["family"] for _, pattern in group_patterns(skeleton)] == []


def test_a_moved_test_file_belongs_to_the_moved_pattern_not_to_the_tests_fold():
    moves = [make_file(f"src/new/__tests__/t{index}.test.ts", "renamed", old_path=f"src/old/__tests__/t{index}.test.ts", no_hunks=True)
             for index in range(4)]
    _, skeleton = build(moves + [make_file("src/new/own.ts", added=source_lines(3))])
    assert [pattern["family"] for _, pattern in group_patterns(skeleton)] == ["moved"]
