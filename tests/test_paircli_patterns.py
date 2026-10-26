import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paircli_patterns import (
    MAX_LABEL_CHARS, after_return, api_surface, api_total, category_members, change_signature, cluster_moved,
    cluster_remainder, cluster_same_change, is_pure_move, label_hint, net_line_set, pattern_label, short_directory, verify_pattern,
)
from snapshot_factory import letters, make_file, make_hunk


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


# --- change_signature: net change, comments ignored --------------------------------------------

def test_signature_comment_only_edit_cancels_out():
    edited = changed("a.ts", removed=["handle(request);"], added=["handle(request); // reviewed"])
    assert change_signature(edited) is None
    pure = changed("a.py", removed=["run()"], added=["run()  # reviewed"])
    assert change_signature(pure) is None
    both = changed("a.ts", removed=["handle(request);"], added=["handle(request); // reviewed", "audit();"])
    assert change_signature(both) == change_signature(changed("b.ts", added=["audit();"]))


def test_signature_comment_appended_to_different_lines_is_the_same():
    first = changed("a.ts", removed=["foo(1);"], added=["foo(1); // reviewed", "audit(1);"])
    second = changed("b.ts", removed=["bar(zzz);"], added=["bar(zzz); // reviewed", "audit(2);"])
    third = changed("c.ts", removed=["baz();"], added=["baz(); // reviewed", "audit(3);"])
    assert change_signature(first) == change_signature(second) == change_signature(third) is not None
    assert change_signature(first) != change_signature(changed("d.ts", added=["audit(3); // x", "other();"]))


def test_signature_large_pr_variants_collapse_to_the_inserted_line_kinds():
    kinds = ["trace(ctx);", "audit(ctx.user);", "metrics.inc('hit');", "if (!ctx) return;"]
    signatures = set()
    for index in range(480):
        existing = f"respond{index % 24}(req, {index});"
        inserted = kinds[index % 4]
        signatures.add(change_signature(changed(
            f"zone{index % 7}/handlers/h{index}.ts", start=1 + index % 9,
            removed=[existing], added=[inserted, existing + " // reviewed"])))
    assert None not in signatures and len(signatures) == 4


def test_signature_keeps_comment_markers_inside_urls_and_strings():
    url = change_signature(changed("a.ts", added=['const home = "https://example.com/a"; // note']))
    assert url == change_signature(changed("b.ts", added=['const home = "https://example.com/a";']))
    assert url != change_signature(changed("c.ts", added=['const home = "https:";']))
    assert change_signature(changed("a.ts", added=["fetch(https://x.dev/api);"])) != change_signature(
        changed("a.ts", added=["fetch(https:"]))
    quoted = change_signature(changed("a.ts", added=['const a = "x // y";']))
    assert quoted != change_signature(changed("a.ts", added=['const a = "x']))
    assert quoted == change_signature(changed("a.ts", added=['const a = "x // y";  // trailing']))
    assert change_signature(changed("a.ts", added=["call();// glued"])) != change_signature(changed("a.ts", added=["call();"]))
    # an unclosed quote keeps the rest of the line
    assert change_signature(changed("a.ts", added=["it's // x"])) != change_signature(changed("a.ts", added=["it's"]))


def test_signature_python_hash_comments():
    assert change_signature(changed("a.py", removed=["x = 1"], added=["x = 1  # reviewed"])) is None
    first = change_signature(changed("a.py", added=["audit()  # added", "# just a note"]))
    assert first == change_signature(changed("b.py", added=["audit()"]))
    string = change_signature(changed("a.py", added=['color = "#fff"']))
    assert string != change_signature(changed("a.py", added=['color = "']))
    assert change_signature(changed("a.py", added=["echo $# args"])) == change_signature(changed("b.py", added=["echo $# args  # c"]))
    assert change_signature(changed("a.sh", added=["echo hi # c"])) == change_signature(changed("a.sh", added=["echo hi"]))
    assert change_signature(changed("a.sql", added=["select 1 -- c"])) == change_signature(changed("a.sql", added=["select 1"]))
    assert change_signature(changed("Makefile", added=["all: build # c"])) == change_signature(changed("Makefile", added=["all: build"]))


def test_signature_pure_comment_files_have_none():
    assert change_signature(changed("a.ts", added=["// reviewed", "   // again", "/* block */", "/**", " * doc", " */"])) is None
    assert change_signature(changed("a.py", added=["# only a note"], removed=["# old note"])) is None
    assert change_signature(changed("a.ts", added=["*ptr = 1;"])) is not None


def test_signature_unknown_extensions_keep_comments():
    marked = change_signature(changed("a.txt", added=["x // y"]))
    assert marked != change_signature(changed("a.txt", added=["x"]))
    assert change_signature(changed("a.txt", removed=["x"], added=["x // y"])) is not None
    assert change_signature(changed("a.md", added=["// note"])) is not None


def test_signature_status_still_matters_with_comments():
    lines = ["audit(); // reviewed"]
    signatures = {change_signature(changed("a.ts", added=lines, status=status)) for status in ("added", "modified", "deleted")}
    assert len(signatures) == 3


def test_signature_digit_only_edit_is_still_a_change():
    bump = changed("package.json", removed=['  "version": "1.2.3",'], added=['  "version": "1.3.0",'])
    assert change_signature(bump) == change_signature(changed("b/package.json", removed=['  "version": "9.9.9",'],
                                                                 added=['  "version": "2.0.0",']))
    assert change_signature(bump) is not None


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


def test_comment_only_files_do_not_join_a_cluster_and_comment_noise_does_not_split_one():
    commented = [changed(f"a/c{index}.ts", removed=[f"call{letters(index)}();"],
                         added=[f"call{letters(index)}(); // reviewed", "audit();"]) for index in range(4)]
    comment_only = [changed(f"b/n{index}.ts", removed=["x();"], added=["x(); // reviewed"]) for index in range(5)]
    (cluster,) = cluster_same_change(commented + comment_only)
    assert cluster["members"] == sorted(file["path"] for file in commented)
    assert (cluster["additions"], cluster["deletions"]) == (8, 4)


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


def test_verify_same_change_member_with_comment_only_edit_has_no_signature():
    pattern, by_path = valid_pattern()
    bad = dict(by_path)
    bad[pattern["members"][0]] = changed(pattern["members"][0], removed=["x();"], added=["x(); // reviewed"])
    errors = verify_pattern(pattern, bad)
    assert list(reasons(errors)) == ["pattern.members[0]"] and "net changed line" in errors[0]["reason"]


def test_verify_remainder_family():
    files = {f"src/f{index}.ts": changed(f"src/f{index}.ts", added=[f"unique{letters(index)}();"]) for index in range(5)}
    members = sorted(files)
    assert verify_pattern({"family": "remainder", "count": 5, "members": members}, files) == []
    assert verify_pattern({"family": "remainder", "count": 3, "members": members[:3]}, files) == []
    # a same-change signature has no place on a remainder row, not even an empty one
    assert "pattern.signature" in reasons(verify_pattern(
        {"family": "remainder", "count": 3, "members": members[:3], "signature": "0" * 16}, files))
    assert "pattern.signature" in reasons(verify_pattern(
        {"family": "remainder", "count": 3, "members": members[:3], "signature": None}, files))
    assert "pattern.members" in reasons(verify_pattern({"family": "remainder", "count": 2, "members": members[:2]}, files))
    assert "pattern.members[1]" in reasons(verify_pattern(
        {"family": "remainder", "count": 3, "members": [members[0], "nope.ts", members[2]]}, files))
    assert "pattern.members[1]" in reasons(verify_pattern(
        {"family": "remainder", "count": 3, "members": [members[0], members[0], members[2]]}, files))
    big = {f"m{index}.ts": changed(f"m{index}.ts", added=["a();"]) for index in range(501)}
    assert verify_pattern({"family": "remainder", "count": 500, "members": sorted(big)[:500]}, big) == []
    assert "pattern.members" in reasons(verify_pattern({"family": "remainder", "count": 501, "members": sorted(big)}, big))


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
    assert pattern_label("remainder", 369) == "369 other changed files"
    assert pattern_label("remainder", 1) == "1 other changed file"
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


def test_api_typescript_a_name_on_both_sides_is_unchanged():
    result = surface("a.ts", added=["export function f(a: string) {}", "export const g = 1;"],
                     removed=["export function f(a: number) {}", "export function h() {}"])
    assert (result["added"], result["removed"]) == (1, 1)
    assert result["added_names"] == ["g"] and result["removed_names"] == ["h"]


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


# --- moved files ----------------------------------------------------------------------------------

def move(index, directory="src/new", old_directory="src/old", **kwargs):
    return make_file(f"{directory}/m{index}.ts", "renamed", old_path=f"{old_directory}/m{index}.ts", no_hunks=True, **kwargs)


def test_a_pure_move_is_a_renamed_file_with_a_different_old_path_and_no_changed_line():
    assert is_pure_move(move(1))
    context_only = make_file("src/new/c.ts", "renamed", old_path="src/old/c.ts", hunks=[make_hunk(1, context=2)])
    assert is_pure_move(context_only), "context lines are not changes"
    assert not is_pure_move(make_file("src/new/e.ts", "renamed", old_path="src/old/e.ts", added=["x();"]))
    assert not is_pure_move(make_file("src/new/e.ts", "renamed", old_path="src/old/e.ts", removed=["x();"]))
    assert not is_pure_move(make_file("src/a.ts", "renamed", old_path="src/a.ts", no_hunks=True)), "same path"
    assert not is_pure_move(make_file("src/a.ts", "renamed", no_hunks=True)), "no old path"
    assert not is_pure_move(make_file("src/a.ts", "renamed", old_path="", no_hunks=True))
    assert not is_pure_move(make_file("src/a.ts", "modified", old_path="src/b.ts", no_hunks=True)), "status matters"
    assert not is_pure_move(make_file("src/a.ts", "added", no_hunks=True))
    for garbage in (None, 3, "x", [], {}, {"path": "a", "status": "renamed", "old_path": "b", "hunks": 7}):
        assert is_pure_move(garbage) in (False, True)  # never raises
    assert is_pure_move({"path": "a", "status": "renamed", "old_path": "b", "hunks": 7})


def test_cluster_moved_needs_three_and_is_pr_wide_and_sorted():
    files = [move(index, directory=f"pkg{index % 2}") for index in range(6)] + [changed("src/edit.ts", added=["a();"])]
    (cluster,) = cluster_moved(files)
    assert cluster["members"] == sorted(file["path"] for file in files[:6]) and cluster["signature"] is None
    assert (cluster["status"], cluster["additions"], cluster["deletions"]) == ("renamed", 0, 0)
    assert cluster_moved(files[:2]) == []
    assert cluster_moved([move(0), move(1), changed("src/e.ts", status="renamed", old_path="x.ts", added=["a"])]) == []
    assert [c["members"] for c in cluster_moved(files, skip={files[0]["path"]})] == [sorted(f["path"] for f in files[1:6])]
    assert cluster_moved([move(0), move(0), move(1), move(2)]) != [] and len(cluster_moved([move(0), move(0), move(1)])) == 0


def test_cluster_moved_splits_above_the_cap_by_directory_and_is_deterministic():
    files = [move(index, directory="src/a", old_directory="old/a") for index in range(300)]
    files += [move(index, directory="src/b", old_directory="old/b") for index in range(300)]
    clusters = cluster_moved(files)
    assert [len(c["members"]) for c in clusters] == [300, 300]
    assert {path.split("/")[1] for path in clusters[0]["members"]} in ({"a"}, {"b"})
    assert clusters == cluster_moved(list(reversed(files)))
    assert cluster_moved(files, max_members=100) and all(len(c["members"]) <= 100 for c in cluster_moved(files, max_members=100))
    assert cluster_moved("nope") == [] and cluster_moved([None, 3]) == []


def test_verify_moved_family():
    files = {file["path"]: file for file in (move(index) for index in range(4))}
    members = sorted(files)
    assert verify_pattern({"family": "moved", "count": 4, "members": members}, files) == []
    assert verify_pattern({"family": "moved", "count": 3, "members": members[:3]}, files) == []
    assert "pattern.members" in reasons(verify_pattern({"family": "moved", "count": 2, "members": members[:2]}, files))
    assert "pattern.signature" in reasons(verify_pattern(
        {"family": "moved", "count": 3, "members": members[:3], "signature": None}, files))
    edited = dict(files)
    edited[members[1]] = make_file(members[1], "renamed", old_path="src/old/m1.ts", added=["x();"])
    assert list(reasons(verify_pattern({"family": "moved", "count": 4, "members": members}, edited))) == ["pattern.members[1]"]
    modified = dict(files)
    modified[members[2]] = make_file(members[2], "modified", old_path="src/old/m2.ts", no_hunks=True)
    assert "pattern.members[2]" in reasons(verify_pattern({"family": "moved", "count": 4, "members": members}, modified))
    same = dict(files)
    same[members[0]] = make_file(members[0], "renamed", old_path=members[0], no_hunks=True)
    assert "pattern.members[0]" in reasons(verify_pattern({"family": "moved", "count": 4, "members": members}, same))
    big = {f"n{index}.ts": make_file(f"n{index}.ts", "renamed", old_path=f"o{index}.ts", no_hunks=True) for index in range(501)}
    assert verify_pattern({"family": "moved", "count": 500, "members": sorted(big)[:500]}, big) == []
    assert "pattern.members" in reasons(verify_pattern({"family": "moved", "count": 501, "members": sorted(big)}, big))


def test_verify_routine_family_takes_one_member_and_only_routine_files():
    files = {"package-lock.json": changed("package-lock.json", added=["x"]), "dist/app.min.js": changed("dist/app.min.js", added=["y"]),
             "src/app.ts": changed("src/app.ts", added=["z"])}
    assert verify_pattern({"family": "routine", "count": 1, "members": ["package-lock.json"]}, files) == []
    assert verify_pattern({"family": "routine", "count": 2, "members": ["package-lock.json", "dist/app.min.js"]}, files) == []
    assert "pattern.members" in reasons(verify_pattern({"family": "routine", "count": 0, "members": []}, files))
    bad = verify_pattern({"family": "routine", "count": 2, "members": ["package-lock.json", "src/app.ts"]}, files)
    assert list(reasons(bad)) == ["pattern.members[1]"] and "not a routine file" in bad[0]["reason"]
    assert "pattern.signature" in reasons(verify_pattern(
        {"family": "routine", "count": 1, "members": ["package-lock.json"], "signature": None}, files))
    assert "pattern.members" in reasons(verify_pattern({"family": "tests", "count": 1, "members": ["tests/a.test.ts"]}, files))


# --- labels ---------------------------------------------------------------------------------------

def test_label_families_moved_and_routine():
    assert pattern_label("moved", 15) == "15 files: moved, not edited"
    assert pattern_label("routine", 164) == "164 routine files" and pattern_label("routine", 1) == "1 routine file"


def test_same_change_label_carries_the_verb_and_a_short_snippet():
    assert pattern_label("same-change", 16, "modified", 'logger.debug("renamed module", { zone: 1 });', "adds") == \
        '16 files: adds `logger.debug("renamed module", { zo\u2026`'
    assert pattern_label("same-change", 40, "deleted") == "40 files: deleted"
    assert pattern_label("same-change", 35, "renamed", None, "edits imports") == "35 files: renamed, edits imports"
    assert pattern_label("same-change", 35, "renamed", "../new/shared", "edits imports") == \
        "35 files: renamed, edits imports of `../new/shared`"
    assert pattern_label("same-change", 8, "modified", "x();", "removes") == "8 files: removes `x();`"
    assert pattern_label("same-change", 8, "modified", "x();", "wipes") == "8 files: same edit", "unknown verb is ignored"
    assert pattern_label("same-change", 8, "modified", "", "adds") == "8 files: adds"
    assert pattern_label("same-change", 8, "modified", "a`b`c", "changes") == "8 files: changes `a'b'c`"
    assert pattern_label("same-change", 8, "modified", "  spaced\n   out  ", "adds") == "8 files: adds `spaced out`"


def test_label_never_exceeds_80_characters_and_shrinks_the_snippet():
    for count in (3, 99, 123456789):
        for status in ("modified", "renamed"):
            for verb in ("adds", "removes", "changes", "adds imports", "removes imports", "edits imports"):
                label = pattern_label("same-change", count, status, "x" * 200, verb)
                assert len(label) <= MAX_LABEL_CHARS, label
                assert label.count("`") in (0, 2)
    assert len(pattern_label("same-change", 5, "modified", "y" * 200, "adds").split("`")[1]) == 36


def test_label_hint_picks_the_longest_net_added_line():
    file = changed("src/a.ts", added=["short();", "logger.debug('a longer distinctive line');", "}"], removed=["gone();"])
    assert label_hint(file) == ("changes", "logger.debug('a longer distinctive line');")
    assert label_hint(changed("src/a.ts", added=["only();", "more_here();"])) == ("adds", "more_here();")
    assert label_hint(changed("src/a.ts", status="deleted", removed=["first();", "second_one();"])) == ("removes", "second_one();")
    assert label_hint(changed("src/a.ts", added=["x();"], removed=["x();"])) is None
    assert label_hint(changed("src/a.ts", added=["}", "});", "{"])) is None, "punctuation alone is no snippet"
    assert label_hint(changed("src/a.ts", added=["x(); // reviewed"], removed=["x();"])) is None, "comment-only edit"
    assert label_hint("junk") is None


def test_label_hint_prefers_code_over_imports_and_names_the_module_when_only_imports_changed():
    mixed = changed("src/a.ts", added=["import { shared } from '../core/shared';", "call();"])
    assert label_hint(mixed) == ("adds", "call();")
    assert label_hint(changed("src/a.ts", added=["import { shared } from '../core/shared';"],
                              removed=["import { shared } from '../old/shared';"])) == ("edits imports", "../core/shared")
    assert label_hint(changed("src/a.ts", removed=["import { shared } from '../old/shared';"])) == \
        ("removes imports", "../old/shared")
    assert label_hint(changed("a.py", added=["from pkg.mod import thing"])) == ("adds imports", "pkg.mod")
    assert label_hint(changed("a.py", added=["import os"])) == ("adds imports", "os")


def test_label_hint_avoids_the_lines_of_a_sibling_when_it_can():
    common = ["const shared_setup = prepare_everything();"]
    first = changed("src/a.ts", added=common + ["extra();"])
    second = changed("src/b.ts", added=common)
    assert label_hint(first)[1] == "const shared_setup = prepare_everything();"
    assert label_hint(first, avoid=net_line_set(second))[1] == "extra();"
    assert label_hint(second, avoid=net_line_set(first))[1] == "const shared_setup = prepare_everything();", "nothing else left"
    assert net_line_set(changed("a.ts", added=["v1();", "w22();"]), raw=True) == {"v1();", "w22();"}
    assert net_line_set(changed("a.ts", added=["v1();"])) == {"v#();"}


# --- after_return ---------------------------------------------------------------------------------

def hunk_of(lines):
    """A hunk from (kind, text) pairs, numbered consecutively; kinds are ctx, add, del."""
    old = new = 1
    items = []
    for kind, text in lines:
        item = {"kind": kind, "text": text}
        if kind != "add":
            item["old_no"], old = old, old + 1
        if kind != "del":
            item["new_no"], new = new, new + 1
        items.append(item)
    return {"old_start": 1, "old_lines": old - 1, "new_start": 1, "new_lines": new - 1, "section": "", "lines": items}


def after(path, lines):
    return after_return(make_file(path, hunks=[hunk_of(lines)]))


def test_after_return_counts_an_added_call_right_after_a_return():
    lines = [("ctx", "function f() {"), ("ctx", "  return 1;"), ("add", "  log('never');"), ("ctx", "}")]
    assert after("src/a.ts", lines) == 1


def test_after_return_counts_a_deleted_call_right_after_a_return_on_the_old_side():
    lines = [("ctx", "function f() {"), ("ctx", "  throw new Error('x');"), ("del", "  cleanup();"), ("ctx", "}")]
    assert after("src/a.ts", lines) == 1


@pytest.mark.parametrize("terminator", ["return", "return value;", "throw err;", "raise ValueError", "break", "continue;"])
def test_after_return_recognises_every_terminator(terminator):
    assert after("src/a.ts", [("ctx", f"    {terminator}"), ("add", "    next();")]) == 1


def test_after_return_skips_blank_and_comment_lines_between():
    lines = [("ctx", "  return 1;"), ("ctx", ""), ("ctx", "  // later"), ("ctx", "  /* more */"), ("ctx", "  * continued"), ("add", "  log();")]
    assert after("src/a.ts", lines) == 1
    assert after("a.py", [("ctx", "    return 1"), ("ctx", "    # note"), ("add", "    log()")]) == 1


def test_after_return_needs_the_same_indentation_and_ignores_new_branches():
    assert after("src/a.ts", [("ctx", "    return 1;"), ("add", "  log();")]) == 0, "outer block: reachable"
    assert after("src/a.ts", [("ctx", "  return 1;"), ("add", "    log();")]) == 0, "deeper: not a plain follower"
    assert after("src/a.ts", [("ctx", "  return 1;"), ("add", "  }")]) == 0
    assert after("src/a.ts", [("ctx", "  return 1;"), ("add", "  } else {")]) == 0
    assert after("src/a.ts", [("ctx", "  break;"), ("add", "  case 2:")]) == 0
    assert after("src/a.ts", [("ctx", "  break;"), ("add", "  default:")]) == 0
    assert after("a.py", [("ctx", "    return 1"), ("add", "    else:")]) == 0
    assert after("a.py", [("ctx", "    return 1"), ("add", "    except KeyError:")]) == 0
    assert after("a.rb", [("ctx", "  return 1"), ("add", "  end")]) == 0
    assert after("src/a.ts", [("ctx", "  return 1;"), ("add", "  ")]) == 0, "blank line"


def test_after_return_looks_at_the_last_code_line_not_the_one_before():
    lines = [("ctx", "  return 1;"), ("add", "  first();"), ("add", "  second();")]
    assert after("src/a.ts", lines) == 1, "only the line that directly follows"
    assert after("src/a.ts", [("add", "  return 1;"), ("ctx", "  other();"), ("add", "  third();")]) == 0


def test_after_return_reads_each_side_on_its_own():
    # old side: return, del-line follows. new side: the return was replaced, so the added line follows `ok();`
    lines = [("ctx", "  return 1;"), ("del", "  old();")]
    assert after("src/a.ts", lines) == 1
    lines = [("del", "  return 1;"), ("add", "  ok();"), ("add", "  more();")]
    assert after("src/a.ts", lines) == 0, "an added line after a deleted return is not after a return"


def test_after_return_first_line_of_a_hunk_and_unknown_languages_count_nothing():
    assert after("src/a.ts", [("add", "  log();"), ("ctx", "  return 1;")]) == 0
    assert after("src/a.md", [("ctx", "return 1;"), ("add", "log();")]) == 0
    assert after("Makefile", [("ctx", "return 1;"), ("add", "log();")]) == 0
    assert after("src/a.c", [("ctx", "return 1;"), ("add", "log();")]) == 0, "only the listed languages"


@pytest.mark.parametrize("path", ["a.ts", "a.tsx", "a.js", "a.jsx", "a.mjs", "a.cjs", "a.go", "a.java", "a.rs", "a.kt", "a.cs", "a.php", "a.swift"])
def test_after_return_languages(path):
    assert after(path, [("ctx", "\treturn x"), ("add", "\tlog()")]) == 1


def test_after_return_sums_over_hunks_and_never_raises():
    hunks = [hunk_of([("ctx", "  return 1;"), ("add", "  a();")]), hunk_of([("ctx", "  throw e;"), ("del", "  b();")])]
    assert after_return(make_file("src/a.ts", hunks=hunks)) == 2
    for garbage in (None, 3, "x", {}, {"path": 5}, {"path": "a.ts", "hunks": 7}, {"path": "a.ts", "hunks": [None, {"lines": 3}]}):
        assert after_return(garbage) == 0


# --- api surface counts the NET change --------------------------------------------------------------

def test_api_comment_only_edit_of_an_export_line_counts_nothing():
    result = surface("a.ts", added=["export function f() {} // reviewed"], removed=["export function f() {}"])
    assert (result["added"], result["removed"]) == (0, 0)
    assert result["added_names"] == [] and result["removed_names"] == []
    python = surface("a.py", added=["def f():  # reviewed"], removed=["def f():"])
    assert (python["added"], python["removed"]) == (0, 0)


def test_api_a_real_rename_still_counts_once_each_way():
    result = surface("a.ts", added=["export function renamed() {}"], removed=["export function original() {}"])
    assert (result["added"], result["removed"]) == (1, 1)
    assert (result["added_names"], result["removed_names"]) == (["renamed"], ["original"])


def test_api_names_are_compared_exactly_digits_are_not_normalised():
    result = surface("a.ts", added=["export const version2 = 1;"], removed=["export const version1 = 1;"])
    assert (result["added"], result["removed"]) == (1, 1)
    go = surface("a.go", added=["func Handler2() {}"], removed=["func Handler1() {}"])
    assert (go["added"], go["removed"]) == (1, 1)


def test_api_a_name_re_listed_identically_on_both_sides_is_unchanged():
    result = surface("a.ts", added=["export { a, b };"], removed=["export { a, c };"])
    assert (result["added_names"], result["removed_names"]) == (["b"], ["c"])
    same = surface("a.ts", added=["export { a, b }; // tidy"], removed=["export { a, b };"])
    assert (same["added"], same["removed"]) == (0, 0)
    go = surface("a.go", added=["func Run(ctx context.Context) error {"], removed=["func Run() error {"])
    assert (go["added"], go["removed"]) == (0, 0), "a changed signature keeps the exported name"


def test_api_total_sums_the_net_counts():
    files = [changed("a.ts", added=["export const x = 1; // c"], removed=["export const x = 1;"]),
             changed("b.ts", added=["export const y = 1;"])]
    assert api_total(files) == {"added": 1, "removed": 0}


# --- cluster_remainder / short_directory ----------------------------------------------------------

def test_a_small_remainder_stays_one_piece_without_a_directory():
    paths = [f"a{index % 5}/f{index}.ts" for index in range(12)]
    assert cluster_remainder(paths) == [(None, sorted(paths))]
    assert cluster_remainder([]) == []


def test_a_large_remainder_clusters_by_the_deepest_directory_with_three_members():
    paths = ([f"pkg/ui/editor/f{index}.ts" for index in range(5)] + [f"pkg/ui/editor/sub/g{index}.ts" for index in range(2)]
             + [f"pkg/ui/menu/h{index}.ts" for index in range(4)] + [f"pkg/server/x{index}.ts" for index in range(3)]
             + [f"root{index}.ts" for index in range(3)])
    pieces = dict(cluster_remainder(paths))
    assert sorted(pieces) == ["", "pkg/server", "pkg/ui/editor", "pkg/ui/menu"]
    assert len(pieces["pkg/ui/editor"]) == 7  # the two files of `sub` walk up to the directory that holds 3 or more
    assert sorted(path for members in pieces.values() for path in members) == sorted(paths)
    assert cluster_remainder(paths) == cluster_remainder(list(reversed(paths)))


def test_clusters_of_fewer_than_three_files_merge_into_a_final_piece_without_a_directory():
    paths = [f"app/big/f{index}.ts" for index in range(10)] + [f"lib{index}/one.ts" for index in range(3)] + ["tools/a.ts", "x/b.ts"]
    pieces = cluster_remainder(paths)
    assert pieces[0][0] == "app/big" and len(pieces[0][1]) == 10
    assert pieces[-1] == (None, ["lib0/one.ts", "lib1/one.ts", "lib2/one.ts", "tools/a.ts", "x/b.ts"])
    assert len(pieces) == 2
    # a single cluster is just the plain row
    assert cluster_remainder([f"app/big/f{index}.ts" for index in range(20)])[0][0] is None


def test_clusters_are_capped_by_merging_the_smallest_first_and_stay_within_500_members():
    paths = [f"d{index:02d}/f{number}.ts" for index in range(40) for number in range(3 + index % 4)]
    pieces = cluster_remainder(paths, 12)
    assert len(pieces) <= 12 and sorted(path for _, members in pieces for path in members) == sorted(paths)
    assert any(name is None for name, _ in pieces) and all(name for name, _ in pieces[:-1])
    assert all(len(members) <= 500 for _, members in pieces)
    one = cluster_remainder(paths, 1)
    assert one == [(None, sorted(paths))]
    big = [f"d{index % 3}/e{index // 3 % 5}/f{index}.ts" for index in range(1300)]
    assert all(len(members) <= 500 for _, members in cluster_remainder(big)) and sum(len(m) for _, m in cluster_remainder(big)) == 1300


def test_short_directory_and_the_remainder_label():
    assert short_directory("components/editor") == "components/editor"
    long = "packages/ade/src/components/editor/notebook/rendering"
    short = short_directory(long)
    assert short == "…/components/editor/notebook/rendering" and len(short) <= 40
    assert len(short_directory("x" * 100)) <= 40
    assert pattern_label("remainder", 12, where="components/editor") == "12 other files in components/editor"
    assert pattern_label("remainder", 12, where="") == "12 other files at the repository root"
    assert pattern_label("remainder", 12) == "12 other changed files"
    assert pattern_label("remainder", 5, where=long).endswith("in " + short) and len(pattern_label("remainder", 5, where="y" * 500)) <= 80
