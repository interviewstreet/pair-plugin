import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paircli_patterns import (
    api_surface, api_total, category_members, change_signature, cluster_same_change, pattern_label, verify_pattern,
)
from snapshot_factory import make_file, make_hunk


def changed(path, added=(), removed=(), status="modified", **kwargs):
    return make_file(path, status=status, added=added, removed=removed, **kwargs)


def surface(path, added=(), removed=()):
    return api_surface(changed(path, added=added, removed=removed))


# --- change_signature ---------------------------------------------------------------------------

def test_signature_ignores_digits_and_paths():
    first = changed("a/one.ts", added=["const legacy00Handler = 1;"], removed=["old00();"])
    second = changed("b/deep/two.ts", added=["const legacy17Handler = 42;"], removed=["old9();"])
    signature = change_signature(first)
    assert signature == change_signature(second)
    assert len(signature) == 16 and int(signature, 16) >= 0


def test_signature_differs_on_non_digit_token_and_on_side():
    base = change_signature(changed("a.ts", added=["const legacy00Handler = 1;"]))
    assert base != change_signature(changed("a.ts", added=["const modern00Handler = 1;"]))
    assert base != change_signature(changed("a.ts", removed=["const legacy00Handler = 1;"]))
    assert base != change_signature(changed("a.ts", added=["const legacy00Handler = 1;", "x();"]))


def test_signature_status_matters_including_renamed():
    lines = ["const a = 1;"]
    signatures = {status: change_signature(changed("a.ts", added=lines, status=status))
                  for status in ("added", "modified", "deleted", "renamed")}
    assert len(set(signatures.values())) == 4
    assert signatures["renamed"] == change_signature(changed("z.ts", added=lines, status="renamed", old_path="q.ts"))


def test_signature_ignores_order_and_repetition_but_not_distinct_lines():
    first = changed("a.ts", added=["x();", "y();", "x();"])
    second = changed("b.ts", added=["y();", "x();", "x();", "x();"])
    third = changed("c.ts", added=["x();", "z();"])
    assert change_signature(first) == change_signature(second)
    assert change_signature(first) != change_signature(third)


def test_signature_trims_whitespace_and_ignores_blank_lines_and_context():
    base = change_signature(changed("a.ts", added=["foo();"]))
    assert base == change_signature(changed("b.ts", added=["   foo();\t", "", "   "]))
    hunk = make_hunk(1, added=["foo();"], context=3)
    assert base == change_signature(changed("c.ts", hunks=[hunk]))


def test_signature_none_without_changed_lines():
    assert change_signature(changed("a.ts", added=["", "   "], removed=["\t"])) is None
    assert change_signature(make_file("a.png", no_hunks=True)) is None
    assert change_signature({"path": "a.ts", "status": "modified"}) is None
    assert change_signature(changed("a.ts", hunks=[make_hunk(1, context=2)])) is None


def test_signature_accepts_line_kind_aliases():
    file = {"path": "a.ts", "status": "modified",
            "hunks": [{"lines": [{"kind": "added", "text": "foo();"}, {"kind": "removed", "text": "bar();"},
                                 {"kind": "context", "text": "x"}]}]}
    assert change_signature(file) == change_signature(changed("b.ts", added=["foo();"], removed=["bar();"]))


@pytest.mark.parametrize("garbage", [
    None, 1, "x", [], {}, {"hunks": None}, {"hunks": "x"}, {"hunks": [None, 3, {"lines": None}]},
    {"status": 5, "hunks": [{"lines": [None, "x", {"kind": 3}, {"kind": "add"}, {"kind": "add", "text": 5}]}]},
])
def test_signature_never_raises(garbage):
    assert change_signature(garbage) is None


def test_signature_performance():
    files = [changed(f"p/file{index}.ts", added=[f"const value{line} = call({index});" for line in range(150)],
                     removed=[f"old{line}();" for line in range(50)]) for index in range(1000)]
    started = time.perf_counter()
    signatures = {change_signature(file) for file in files}
    assert time.perf_counter() - started < 1.5
    assert len(signatures) == 1


# --- cluster_same_change ------------------------------------------------------------------------

def deleted_template(count, directory="src/legacy"):
    return [changed(f"{directory}/area{index}/module{index}.ts", status="deleted",
                    hunks=[make_hunk(1, removed=[f"export const legacy{index}Handler = {index};",
                                                              "export default legacy;"], context=0)])
            for index in range(count)]


def test_deleted_template_cluster_of_forty():
    files = deleted_template(40) + [changed("src/other.ts", added=["const unrelated = 1;"])]
    clusters = cluster_same_change(files)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["status"] == "deleted"
    assert cluster["members"] == sorted(file["path"] for file in files[:40])
    assert (cluster["additions"], cluster["deletions"]) == (0, 80)
    assert cluster["signature"] == change_signature(files[0])
    assert set(cluster) == {"signature", "status", "members", "additions", "deletions"}


def test_modified_files_cluster_and_mixed_status_not_merged():
    modified = [changed(f"svc/s{index}.py", added=[f'VERSION = "1.2.{index}"'], removed=['VERSION = "1.1.0"'])
                for index in range(6)]
    # a sibling with the same lines but another status is a different cluster of its own
    added = [changed(f"new/n{index}.py", status="added", added=[f'VERSION = "1.2.{index}"'],
                     removed=['VERSION = "1.1.0"']) for index in range(3)]
    clusters = cluster_same_change(modified + added)
    assert [(cluster["status"], len(cluster["members"])) for cluster in clusters] == [("modified", 6), ("added", 3)]
    assert (clusters[0]["additions"], clusters[0]["deletions"]) == (6, 6)


def test_cluster_below_min_members_dropped_and_none_signatures_ignored():
    files = deleted_template(2) + [make_file(f"img{index}.png", no_hunks=True) for index in range(5)]
    assert cluster_same_change(files) == []
    assert len(cluster_same_change(deleted_template(2), min_members=2)) == 1


def test_cluster_split_over_max_is_deterministic_and_complete():
    files = deleted_template(1100)
    clusters = cluster_same_change(files)
    assert clusters == cluster_same_change(list(reversed(files)))
    members = [path for cluster in clusters for path in cluster["members"]]
    assert sorted(members) == sorted(file["path"] for file in files)
    assert len(members) == len(set(members))
    assert all(3 <= len(cluster["members"]) <= 500 for cluster in clusters)
    assert len({cluster["signature"] for cluster in clusters}) == 1
    sizes = [len(cluster["members"]) for cluster in clusters]
    assert sizes == sorted(sizes, reverse=True)
    assert sum(cluster["deletions"] for cluster in clusters) == 2200


def test_cluster_split_follows_directories():
    files = [changed(f"{top}/f{index}.ts", added=["bump();"]) for top in ("a", "b", "c") for index in range(4)]
    clusters = cluster_same_change(files, max_members=4)
    assert [cluster["members"] for cluster in clusters] == [
        [f"a/f{i}.ts" for i in range(4)], [f"b/f{i}.ts" for i in range(4)], [f"c/f{i}.ts" for i in range(4)],
    ]


def test_cluster_split_packs_small_directories_and_chunks_flat_ones():
    files = [changed(f"d{top}/f{index}.ts", added=["bump();"]) for top in range(4) for index in range(3)]
    clusters = cluster_same_change(files, max_members=7)
    assert sorted(len(cluster["members"]) for cluster in clusters) == [6, 6]
    flat = [changed(f"flat/f{index:03d}.ts", added=["bump();"]) for index in range(10)]
    clusters = cluster_same_change(flat, max_members=4)
    assert [len(cluster["members"]) for cluster in clusters] == [4, 3, 3]


def test_cluster_skip_and_duplicates():
    files = deleted_template(5)
    skip = {files[0]["path"], files[1]["path"], files[2]["path"]}
    assert cluster_same_change(files, skip=skip) == []
    clusters = cluster_same_change(files, skip={files[0]["path"]})
    assert len(clusters[0]["members"]) == 4
    assert len(cluster_same_change(files + files)[0]["members"]) == 5


def test_cluster_ordering_largest_first_then_signature():
    big = [changed(f"big/b{index}.ts", added=["big();"]) for index in range(5)]
    small_a = [changed(f"sa/a{index}.ts", added=["one();"]) for index in range(3)]
    small_b = [changed(f"sb/b{index}.ts", added=["two();"]) for index in range(3)]
    clusters = cluster_same_change(small_b + small_a + big)
    assert [len(cluster["members"]) for cluster in clusters] == [5, 3, 3]
    assert clusters[1]["signature"] < clusters[2]["signature"]
    assert clusters == cluster_same_change(big + small_a + small_b)


def test_cluster_robust_to_garbage():
    assert cluster_same_change(None) == []
    assert cluster_same_change([None, 3, {}, {"path": 5}, {"path": "a"}]) == []
    assert cluster_same_change(deleted_template(3), skip=[1, None]) != []


# --- category_members ---------------------------------------------------------------------------

def test_category_members():
    files = [changed(f"tests/unit/t{index}.py", added=["x"]) for index in range(3)]
    files += [changed("src/a.py", added=["x"]), changed("README.md", added=["x"]),
              changed("docs/a.md", added=["x"]), changed("docs/b.md", added=["x"]),
              changed("config/app.yaml", added=["x"]), changed("package-lock.json", added=["x"])]
    assert category_members(files, "tests") == ["tests/unit/t0.py", "tests/unit/t1.py", "tests/unit/t2.py"]
    assert category_members(files, "docs") == ["README.md", "docs/a.md", "docs/b.md"]
    assert category_members(files, "docs", min_members=4) == []
    assert category_members(files, "config") == []
    assert category_members(files, "config", min_members=1) == ["config/app.yaml"]
    assert category_members(files, "source") == []
    assert category_members(files, "tests", skip={"tests/unit/t0.py"}) == []
    assert category_members(None, "tests") == []
    assert category_members([None, {"path": 3}], "tests") == []


# --- verify_pattern -----------------------------------------------------------------------------

def valid_pattern(count=4):
    files = deleted_template(count)
    by_path = {file["path"]: file for file in files}
    members = sorted(by_path)
    return ({"family": "same-change", "count": count, "signature": change_signature(files[0]), "members": members},
            by_path)


def reasons(errors):
    return {error["path"]: error["reason"] for error in errors}


def test_verify_valid_same_change():
    pattern, by_path = valid_pattern()
    assert verify_pattern(pattern, by_path) == []


def test_verify_same_change_errors():
    pattern, by_path = valid_pattern()
    assert "pattern.signature" in reasons(verify_pattern({**pattern, "signature": ""}, by_path))
    bad = dict(by_path)
    bad[pattern["members"][1]] = changed(pattern["members"][1], status="deleted", removed=["different();"])
    errors = verify_pattern(pattern, bad)
    assert list(reasons(errors)) == ["pattern.members[1]"]
    assert "signature" in errors[0]["reason"]
    bad[pattern["members"][2]] = make_file(pattern["members"][2], no_hunks=True)
    assert "pattern.members[2]" in reasons(verify_pattern(pattern, bad))
    wrong_signature = verify_pattern({**pattern, "signature": "0" * 16}, by_path)
    assert len(wrong_signature) == 4


def test_verify_structure_errors():
    pattern, by_path = valid_pattern()
    members = pattern["members"]
    assert "pattern.count" in reasons(verify_pattern({**pattern, "count": 5}, by_path))
    assert "pattern.count" in reasons(verify_pattern({**pattern, "count": True}, by_path))
    assert "pattern.count" in reasons(verify_pattern({k: v for k, v in pattern.items() if k != "count"}, by_path))
    duplicate = verify_pattern({**pattern, "members": members + [members[0]], "count": 5}, by_path)
    assert reasons(duplicate) == {"pattern.members[4]": f"duplicate member {members[0]}"}
    missing = verify_pattern({**pattern, "members": members[:3] + ["nope.ts"]}, by_path)
    assert list(reasons(missing)) == ["pattern.members[3]"]
    assert "not a changed file" in missing[0]["reason"]
    assert "pattern.members" in reasons(verify_pattern({**pattern, "members": members[:2], "count": 2}, by_path))
    assert "pattern.family" in reasons(verify_pattern({**pattern, "family": "bulk"}, by_path))
    assert "pattern.family" in reasons(verify_pattern({k: v for k, v in pattern.items() if k != "family"}, by_path))
    not_strings = verify_pattern({**pattern, "members": members[:3] + [7, ""], "count": 5}, by_path)
    assert {"pattern.members[3]", "pattern.members[4]"} <= set(reasons(not_strings))


def test_verify_member_count_bounds():
    files = [changed(f"tests/t{index}.ts", added=["x"]) for index in range(501)]
    by_path = {file["path"]: file for file in files}
    members = sorted(by_path)
    assert verify_pattern({"family": "tests", "count": 500, "members": members[:500]}, by_path) == []
    errors = verify_pattern({"family": "tests", "count": 501, "members": members}, by_path)
    assert list(reasons(errors)) == ["pattern.members"]
    assert verify_pattern({"family": "tests", "count": 3, "members": members[:3]}, by_path) == []


def test_verify_category_families():
    files = ([changed(f"tests/t{index}.ts", added=["x"]) for index in range(3)]
             + [changed("src/a.ts", added=["x"])] + [changed(f"docs/d{index}.md", added=["x"]) for index in range(3)]
             + [changed(f"cfg/c{index}.yaml", added=["x"]) for index in range(3)])
    by_path = {file["path"]: file for file in files}
    tests = sorted(path for path in by_path if path.startswith("tests/"))
    docs = sorted(path for path in by_path if path.startswith("docs/"))
    config = sorted(path for path in by_path if path.startswith("cfg/"))
    for family, members in (("tests", tests), ("docs", docs), ("config", config)):
        assert verify_pattern({"family": family, "count": 3, "members": members}, by_path) == []
    wrong = verify_pattern({"family": "tests", "count": 3, "members": tests[:2] + ["src/a.ts"]}, by_path)
    assert list(reasons(wrong)) == ["pattern.members[2]"]
    assert "not a tests file" in wrong[0]["reason"]
    assert verify_pattern({"family": "docs", "count": 3, "members": tests}, by_path)


@pytest.mark.parametrize("pattern", [None, 3, "x", [], {}, {"family": "tests", "members": "abc"},
                                     {"family": "tests", "members": None}])
def test_verify_garbage_pattern(pattern):
    errors = verify_pattern(pattern, {})
    assert errors and all(set(error) == {"path", "reason"} for error in errors)


def test_verify_garbage_files_by_path():
    pattern, _ = valid_pattern()
    assert len(verify_pattern(pattern, None)) == 4
    assert len(verify_pattern(pattern, {path: None for path in pattern["members"]})) == 4


def test_verify_caps_errors():
    members = [f"m{index}.ts" for index in range(500)]
    errors = verify_pattern({"family": "tests", "count": 500, "members": members}, {})
    assert len(errors) == 100


# --- pattern_label ------------------------------------------------------------------------------

def test_pattern_labels():
    assert pattern_label("same-change", 40, "deleted") == "40 files: deleted"
    assert pattern_label("same-change", 35, "renamed") == "35 files: renamed and edited"
    assert pattern_label("same-change", 8, "modified") == "8 files: same edit"
    assert pattern_label("same-change", 8, "added") == "8 files: added"
    assert pattern_label("same-change", 8) == "8 files: same change"
    assert pattern_label("tests", 12) == "12 test files"
    assert pattern_label("docs", 6) == "6 doc files"
    assert pattern_label("config", 4) == "4 config files"
    assert pattern_label("tests", 1) == "1 test file"
    assert pattern_label("bogus", 5) == "5 files"
    assert pattern_label(None, None, 3) == "0 files"
    assert len(pattern_label("same-change", 10 ** 100, "modified")) <= 80


# --- api_surface --------------------------------------------------------------------------------

def test_api_typescript_declaration_forms():
    result = surface("src/a.ts", added=[
        "export function alpha() {}", "export async function beta() {}", "export class Gamma {}",
        "export abstract class Delta {}", "export const epsilon = 1;", "export let zeta = 2;", "export var eta = 3;",
        "export type Theta = string;", "export interface Iota {}", "export enum Kappa {}",
        "export const enum Lambda {}", "export declare function mu(): void;", "export default function () {}",
        "  export namespace Nu {}", "export function* xi() {}",
    ])
    assert result["removed"] == 0
    assert result["added"] == 15
    assert result["added_names"] == [
        "alpha", "beta", "Gamma", "Delta", "epsilon", "zeta", "eta", "Theta", "Iota", "Kappa", "Lambda", "mu",
        "default", "Nu", "xi"]


def test_api_typescript_non_exports_ignored():
    result = surface("src/a.tsx", added=[
        "function internal() {}", "const exported = 1;", "// export function commented() {}",
        "  * export const doc = 1;", "import { x } from './x';", "const s = 'export const nope = 1';",
        "export const { a, b } = obj;", "export = legacy;", "module.exports = {};",
    ])
    assert result == {"added": 0, "removed": 0, "added_names": [], "removed_names": []}
    assert surface("a.ts", added=["export const enumeration = 1;"])["added_names"] == ["enumeration"]


def test_api_typescript_brace_and_star_exports():
    result = surface("src/index.ts", added=[
        "export { a, b as c } from './x';", "export { type D, type E as F, g };", "export type { H, I };",
        "export * from './all';", "export * as ns from './ns';", "export {};",
    ])
    assert result["added_names"] == ["a", "c", "D", "F", "g", "H", "I", "*", "ns"]
    assert result["added"] == 9


def test_api_typescript_multiline_brace_export():
    result = surface("src/index.ts", added=["export {", "  one,", "  two as three, // note", "} from './x';", "foo();"])
    assert result["added_names"] == ["one", "three"]
    # a block that starts on a context line is not a changed export
    hunk = make_hunk(1, added=["  extra,"], context=0)
    hunk["lines"] = ([{"kind": "ctx", "text": "export {", "old_no": 1, "new_no": 1}] + hunk["lines"]
                     + [{"kind": "ctx", "text": "} from './x';", "old_no": 2, "new_no": 3}])
    assert api_surface(changed("src/index.ts", hunks=[hunk]))["added"] == 0


def test_api_javascript_extensions():
    for path in ("a.js", "a.jsx", "a.mjs", "a.cjs", "a.mts", "a.cts", "a.ts", "a.tsx"):
        assert surface(path, added=["export const one = 1;"])["added"] == 1, path


def test_api_typescript_removed_and_readded_counts_both():
    result = surface("a.ts", added=["export function f(a: string) {}", "export const g = 1;"],
                     removed=["export function f(a: number) {}", "export function h() {}"])
    assert (result["added"], result["removed"]) == (2, 2)
    assert result["removed_names"] == ["f", "h"]


def test_api_names_counted_once_per_side():
    result = surface("a.ts", added=["export function f(a: A): void;", "export function f(a: B): void;"])
    assert result["added"] == 1


def test_api_python():
    result = surface("pkg/mod.py", added=[
        "def public():", "async def fetch():", "class Service:", "def _private():", "class _Hidden:",
        "    def method(self):", "\tclass Inner:", "    async def inner():", "define = 3", "classy = 1",
        "def __dunder__():",
    ], removed=["def gone():", "def _gone():"])
    assert result["added_names"] == ["public", "fetch", "Service"]
    assert (result["added"], result["removed"], result["removed_names"]) == (3, 1, ["gone"])


def test_api_go():
    result = surface("pkg/server.go", added=[
        "func Serve(addr string) error {", "func helper() {}", "func (s *Server) Start() error {",
        "func (Server) Name() string {", "func (s *Server) stop() {}", "func (c Cache[K, V]) Get(key K) V {",
        "func Map[T any](xs []T) []T {", "type Handler interface {", "type options struct {",
        "var Default = New()", "var internal = 1", "const Version = \"1.0\"", "const maxRetries = 3",
        "var A, B = 1, 2", "var x, Y int", "\tfunc Nested() {}",
    ], removed=["func Old() {}", "func (o *Thing) Do() {}"])
    assert result["added_names"] == [
        "Serve", "Server.Start", "Server.Name", "Cache.Get", "Map", "Handler", "Default", "Version", "A", "B", "Y"]
    assert result["added"] == 11
    assert (result["removed"], result["removed_names"]) == (2, ["Old", "Thing.Do"])


def test_api_go_methods_on_different_receivers_are_distinct():
    result = surface("a.go", added=["func (a A) String() string {", "func (b B) String() string {"])
    assert result["added_names"] == ["A.String", "B.String"]


def test_api_go_blocks():
    result = surface("a.go", added=[
        "var (", "\tDefault = New()", "\tprivate = 1", "\tA, B = 1, 2", "\tTable = map[string]int{",
        "\t\tKey: 1,", "\t}", ")", "const (", "\tModeA = iota", "\tModeB", ")", "Stray = 1",
    ])
    assert result["added_names"] == ["Default", "A", "B", "Table", "ModeA", "ModeB"]


def test_api_go_block_context_only_when_opening_visible():
    hunk = make_hunk(1, added=["\tNewName = 2"], context=0)
    hunk["lines"] = ([{"kind": "ctx", "text": "var (", "old_no": 1, "new_no": 1},
                      {"kind": "ctx", "text": "\tExisting = 1", "old_no": 2, "new_no": 2}] + hunk["lines"])
    result = api_surface(changed("a.go", hunks=[hunk]))
    assert result["added_names"] == ["NewName"]
    # without the opening line in the hunk, an indented name is not seen
    assert surface("a.go", added=["\tLonely = 1"])["added"] == 0


def test_api_go_block_does_not_leak_between_sides_or_hunks():
    first = make_hunk(1, added=["var ("], removed=[], context=0)
    second = make_hunk(40, added=["\tFree = 1"], context=0)
    assert api_surface(changed("a.go", hunks=[first, second]))["added"] == 0
    removed_block = make_hunk(1, removed=["const (", "\tOld = 1", ")"], added=["\tNew = 1"], context=0)
    result = api_surface(changed("a.go", hunks=[removed_block]))
    assert (result["added"], result["removed"]) == (0, 1)


def test_api_unknown_language_is_zero():
    for path in ("a.rs", "a.java", "README.md", "a.sql", "Makefile", "data.json"):
        assert surface(path, added=["export function f() {}", "def f():", "func F() {}", "pub fn f() {}"]) == {
            "added": 0, "removed": 0, "added_names": [], "removed_names": []}


def test_api_names_capped_at_twenty_but_count_is_full():
    result = surface("a.ts", added=[f"export const name{index} = 1;" for index in range(30)],
                     removed=[f"export const old{index} = 1;" for index in range(25)])
    assert (result["added"], result["removed"]) == (30, 25)
    assert len(result["added_names"]) == len(result["removed_names"]) == 20
    assert result["added_names"][0] == "name0"


def test_api_total():
    files = [surface_file for surface_file in (
        changed("a.ts", added=["export const a = 1;"], removed=["export const b = 1;", "export const c = 1;"]),
        changed("b.py", added=["def f():", "class C:"]),
        changed("c.rs", added=["pub fn x() {}"]),
    )]
    assert api_total(files) == {"added": 3, "removed": 2}
    assert api_total([]) == {"added": 0, "removed": 0}
    assert api_total(None) == {"added": 0, "removed": 0}
    assert api_total([None, 3, {}]) == {"added": 0, "removed": 0}


@pytest.mark.parametrize("garbage", [
    None, 1, "x", [], {}, {"path": None}, {"path": "a.ts", "hunks": "x"}, {"path": "a.ts", "hunks": [None, {"lines": 3}]},
    {"path": "a.go", "hunks": [{"lines": [{"kind": "add", "text": None}, {"kind": "add"}, 5]}]},
])
def test_api_surface_never_raises(garbage):
    assert api_surface(garbage) == {"added": 0, "removed": 0, "added_names": [], "removed_names": []}


# --- determinism --------------------------------------------------------------------------------

def test_determinism_and_order_independence():
    files = deleted_template(6) + [changed(f"tests/t{index}.py", added=["assert True"]) for index in range(4)]
    first = cluster_same_change(files)
    assert first == cluster_same_change(files)
    assert first == cluster_same_change(list(reversed(files)))
    assert category_members(files, "tests") == category_members(list(reversed(files)), "tests")
    assert [change_signature(file) for file in files] == [change_signature(file) for file in files]
