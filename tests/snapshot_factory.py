"""Synthetic snapshot.json builders with realistic hunks, for the skeleton and diagram tests.

Hunk lines match Pair CLI's real shape: {"kind": "add" | "del" | "ctx", "text": "<no +/- prefix>",
"old_no"?: int, "new_no"?: int} (add lines carry only new_no, del lines only old_no).
"""
from __future__ import annotations

HEAD = "a" * 40


def make_hunk(start: int, added=(), removed=(), context: int = 1, new_file: bool = False) -> dict:
    """One hunk at line `start`: context, removed lines, added lines, context.

    A new file has no old side (old_start 0). With context 0 and only removals the hunk has no new
    side (new_start 0, new_lines 0), which is how git reports a deleted file.
    """
    lines, old_no, new_no = [], start, start
    leading = 0 if new_file else context
    trailing = 0 if new_file else context
    for index in range(leading):
        lines.append({"kind": "ctx", "text": f"// context before {index}", "old_no": old_no, "new_no": new_no})
        old_no += 1
        new_no += 1
    for text in removed:
        lines.append({"kind": "del", "text": text, "old_no": old_no})
        old_no += 1
    for text in added:
        lines.append({"kind": "add", "text": text, "new_no": new_no})
        new_no += 1
    for index in range(trailing):
        lines.append({"kind": "ctx", "text": f"// context after {index}", "old_no": old_no, "new_no": new_no})
        old_no += 1
        new_no += 1
    return {
        "old_start": 0 if new_file else start, "old_lines": 0 if new_file else old_no - start,
        "new_start": start if new_no > start else 0, "new_lines": new_no - start, "section": "", "lines": lines,
    }


def make_file(path: str, status: str = "modified", added=(), removed=(), start: int = 1,
              old_path: str | None = None, hunks: list | None = None, no_hunks: bool = False) -> dict:
    """A changed file. Pass `hunks` (from make_hunk) for several hunks, or no_hunks for a patch-less file."""
    entry = {"path": path, "status": status}
    if old_path is not None:
        entry["old_path"] = old_path
    if no_hunks:
        entry["hunks"] = []
        return entry
    if hunks is None:
        hunks = [make_hunk(start, added, removed, context=0 if status == "deleted" else 1,
                           new_file=status == "added")]
    entry["hunks"] = hunks
    return entry


def make_snapshot(files: list[dict], head: str = HEAD, commits: int = 1, title: str = "Synthetic change") -> dict:
    """A snapshot whose last commit is `head`, as `paircli scan --snapshot` writes it."""
    shas = [f"{index:040x}" for index in range(1, commits)] + [head]
    return {"pr": {
        "repo": "acme/repo", "number": 42, "url": "https://github.com/acme/repo/pull/42", "title": title,
        "body": "", "head_ref": "feature", "base_ref": "main", "created_at": "2026-01-01T00:00:00Z",
        "commits": [{"sha": sha} for sha in shas], "files": files,
    }}


def source_lines(count: int, prefix: str = "const value") -> list[str]:
    """`count` plain code lines."""
    return [f"{prefix}{index} = {index};" for index in range(count)]


def many_files(count: int, directories: list[str] | None = None, lines: int = 6, extension: str = "ts") -> list[dict]:
    """`count` modified files spread over `directories` (default: a few nested ones), `lines` added lines each."""
    directories = directories or [f"packages/pkg{index % 7}/src/mod{index % 5}" for index in range(40)]
    return [
        make_file(f"{directories[index % len(directories)]}/file{index}.{extension}",
                  added=source_lines(lines), removed=source_lines(1, "old"), start=1 + index % 3)
        for index in range(count)
    ]


# --- repeated-change PRs (pattern rows) ---------------------------------------------------------

def deleted_templates(count: int, directory: str = "src/legacy/templates") -> list[dict]:
    """`count` deleted files that differ only in digits, so they share one change signature."""
    return [
        make_file(f"{directory}/t{index}.ts", "deleted", start=1,
                  removed=[f"// template {index}", "export function render() {", f"  return '<div id=\"t{index}\"/>';", "}"])
        for index in range(count)
    ]


def deletion_heavy_pr(templates: int = 40, features: int = 9) -> list[dict]:
    """Template deletions, an index that stopped re-exporting them, and features that drop one shared import."""
    index = make_file("src/legacy/index.ts", added=["export const ready = true;"],
                      removed=[f"export * from './templates/t{number}';" for number in range(templates)])
    users = [
        make_file(f"src/features/f{number}.ts", start=1, added=["// no legacy"],
                  removed=["import { render } from '../legacy/index';"])
        for number in range(features)
    ]
    return deleted_templates(templates) + [index] + users


def version_bumps(count: int, directory: str = "packages") -> list[dict]:
    """`count` package.json files with the same version-line change (digits aside)."""
    return [
        make_file(f"{directory}/pkg{index}/package.json", start=3,
                  removed=['  "version": "1.2.3",'], added=['  "version": "1.3.0",'])
        for index in range(count)
    ]


def repeated_edits(count: int, variants: int = 3, directory: str = "src/modules") -> list[dict]:
    """`count` modified files that each get one of `variants` distinct edits, whatever their number."""
    names = ["Logger", "Tracer", "Meter", "Clock", "Cache"]
    return [
        make_file(f"{directory}/area{index % 8}/mod{index}.ts", start=2,
                  removed=[f"import {{ log }} from '{names[index % variants]}';  // v1"],
                  added=[f"import {{ log }} from '{names[index % variants]}2';  // v{index}"])
        for index in range(count)
    ]
