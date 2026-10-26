"""Deterministic PR skeleton and a fallback ADE v1 diagram built from it.

The model narrates, code computes: everything here uses only the hunks in snapshot.json. There
is no model, no network and no repository read, and the same input always gives the same output.

`build_skeleton(snapshot, head)` returns a plain, JSON-serializable dict:

  head, head_bound      the head SHA and whether the snapshot's commits vouch for it
  totals                {files, additions, deletions, routine_files}
  files                 one record per changed path (the last snapshot entry wins on repeats):
                        path, old_path, status, additions, deletions, language, category,
                        sensitive, routine, lines_available, has_new_lines (a new-side hunk
                        exists), score, group (group id; None for routine files with no group)
  groups                directory clusters, best first (score desc, path asc): id, label, path,
                        files (non-routine paths, ranked), routine_count, additions, deletions
                        (non-routine files only), sensitive, change, score, citable_files
  routine_unassigned    routine files that belong to no group
  edges                 candidate import edges between changed files, every one read from an
                        import line in a hunk: source_file, target_file, source_group,
                        target_group, cite {path, line, side}, kind "import". The v1 diagram
                        uses only side "new" edges; "old" edges come from removed lines.
                        A target is a file only when resolution is unambiguous: a Go import
                        names a package, so it is a file edge only when that package has exactly
                        one changed non-test .go file. Otherwise the edge is package-level
                        (`target_file` None, plus `target_package` and `target_anchor`) and serves
                        group aggregation only. Ambiguous Python or alias matches give no edge.
  budget                size_budget() for this PR (non-routine files and changed lines)
  docs_only             no files, or every non-routine file is documentation (and one exists)
  no_citable_lines      no file has a new-side hunk line to cite

`deterministic_architecture(skeleton, head, snapshot)` turns that into an ADE v1 document, or
returns None when nothing can be cited.

Size budget (`size_budget`, by non-routine file count; changed_lines >= 2000 moves one tier up):

  files    max_groups  max_rows_per_group  max_edges
  0-1           1             12                0
  2-3           3             12                6
  4-8           5             12               10
  9-20          8             12               16
  21-50        12             10               24
  51-120       16              8               30
  121+         20              8               30

Ranking: score = log(1 + changed lines) + 5 for a sensitive path - a penalty for tests (2),
docs (3) and config (1.5), so a source change outranks the same-size test, docs or config change.
Groups use their total lines, any sensitive file, and the penalty of their largest category.
"""
from __future__ import annotations

import math
import posixpath
import re

from paircli_snapshot import lines_available, snapshot_binds_head

# The same lockfile names the fallback summary treats as safe to skip.
LOCK_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "go.sum",
    "uv.lock", "gemfile.lock", "composer.lock", "pipfile.lock",
}
EXTRA_LOCK_FILES = {"npm-shrinkwrap.json", "bun.lockb"}
SENSITIVE_WORDS = (
    "auth", "secret", "token", "credential", "password", "migration", "schema", "permission",
    ".env", "workflow", "dockerfile", "security",
)
LANGUAGES = {
    "ts": "ts", "mts": "ts", "cts": "ts", "tsx": "tsx", "js": "js", "mjs": "js", "cjs": "js",
    "jsx": "jsx", "py": "py", "go": "go", "java": "java", "rb": "rb", "rs": "rs",
    "css": "css", "scss": "css", "less": "css", "md": "md", "mdx": "md", "markdown": "md",
    "json": "json", "yaml": "yaml", "yml": "yaml", "sql": "sql", "sh": "sh", "bash": "sh", "zsh": "sh",
}
BINARY_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "ico", "webp", "bmp", "svg", "pdf", "zip", "gz", "tar", "tgz", "jar",
    "woff", "woff2", "ttf", "otf", "eot", "mp3", "mp4", "mov", "wasm", "bin", "exe", "dll", "so",
    "dylib", "class", "pyc",
}
DOC_EXTENSIONS = {"md", "mdx", "markdown", "rst", "txt", "adoc"}
DOC_DIRECTORIES = {"docs", "doc", "documentation"}
DOC_NAMES = {"readme", "license", "changelog", "contributing", "authors", "notice", "codeowners"}
CONFIG_EXTENSIONS = {"json", "yaml", "yml", "toml", "ini", "cfg", "conf", "env", "properties", "xml"}
CONFIG_NAMES = {"dockerfile", "makefile", "procfile", "gemfile", "rakefile", "jenkinsfile", "vagrantfile"}
TEST_DIRECTORIES = {"test", "tests", "__tests__", "spec", "specs", "e2e", "__mocks__", "testdata", "testing"}
VENDOR_DIRECTORIES = {"vendor", "node_modules", "third_party", "third-party", "bower_components", "site-packages"}
GENERATED_DIRECTORIES = {
    "generated", "__generated__", "dist", "build", "out", ".next", ".nuxt", ".turbo", "coverage", "__snapshots__",
}
GENERATED_SUFFIXES = ("_pb2.py", "_pb2_grpc.py", ".pb.go", ".pb.ts", ".d.ts.map")
ROUTINE_CATEGORIES = {"lockfile", "generated", "vendor", "binary"}
CATEGORY_PENALTY = {"source": 0.0, "test": 2.0, "docs": 3.0, "config": 1.5}
SENSITIVE_BOOST = 5.0

# Diagram limits mirror validate_architecture; the tests prove each document validates.
MAX_NODES = 20
MAX_DIAGRAM_EDGES = 30
MAX_EVIDENCE = 12
MAX_NODE_FILES = 12  # the contract allows 15, but every listed file needs its own citation
MAX_LAYER = 8
MAX_PATH_CHARS = 300
MAX_GAPS = 5
MAX_GAP_CHARS = 300
MAX_SKELETON_EDGES = 2000
MIN_SPLIT_SIZE = 5
LINES_BUMP = 2000
# (largest non-routine file count, max_groups, max_rows_per_group); None means no upper limit.
BUDGET_TIERS = ((1, 1, 12), (3, 3, 12), (8, 5, 12), (20, 8, 12), (50, 12, 10), (120, 16, 8), (None, 20, 8))
OTHER_LABEL = "Other changes"
ROOT_LABEL = "Repository root"

JS_LANGUAGES = {"ts", "tsx", "js", "jsx"}
JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs")
JS_FROM = re.compile(r"""^\s*(?:import|export)\b.*?\bfrom\s*['"]([^'"]+)['"]""")
JS_CLOSE = re.compile(r"""^\s*\}?\s*from\s*['"]([^'"]+)['"]""")
JS_SIDE_EFFECT = re.compile(r"""^\s*import\s*['"]([^'"]+)['"]""")
JS_CALL = re.compile(r"""\b(?:require|import)\s*\(\s*['"]([^'"]+)['"]\s*\)""")
PY_FROM = re.compile(r"^\s*from\s+(\.*)([A-Za-z_][\w.]*)?\s+import\s+(.*)$")
PY_IMPORT = re.compile(r"^\s*import\s+(.+)$")
PY_NAME = re.compile(r"[A-Za-z_]\w*")
GO_SINGLE = re.compile(r'^\s*import\s+(?:[\w.]+\s+)?"([^"]+)"')
GO_BLOCK_LINE = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"\s*(?://.*)?$')
GO_STD_ROOTS = {
    "archive", "bufio", "bytes", "cmp", "compress", "container", "context", "crypto", "database", "debug",
    "embed", "encoding", "errors", "expvar", "flag", "fmt", "go", "hash", "html", "image", "index", "io",
    "iter", "log", "maps", "math", "mime", "net", "os", "path", "plugin", "reflect", "regexp", "runtime",
    "slices", "sort", "strconv", "strings", "structs", "sync", "syscall", "testing", "text", "time",
    "unicode", "unique", "unsafe",
}
LINE_KINDS = {
    "add": "add", "added": "add", "+": "add",
    "del": "del", "delete": "del", "deleted": "del", "remove": "del", "removed": "del", "-": "del",
    "ctx": "ctx", "context": "ctx", " ": "ctx",
}
STATUSES = {"added", "modified", "deleted", "renamed"}


def _int(value):
    return value if type(value) is int else None


def _clean(value, limit: int | None = None) -> str:
    """Collapse whitespace and control characters so snapshot text cannot break a sentence."""
    text = re.sub(r"[\x00-\x1f\x7f\s]+", " ", value if isinstance(value, str) else "").strip()
    return text if limit is None or len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def size_budget(n_files: int, changed_lines: int) -> dict:
    """Return {max_groups, max_rows_per_group, max_edges} for a PR of this size (table above)."""
    count = max(0, _int(n_files) or 0)
    lines = max(0, _int(changed_lines) or 0)
    tier = next(index for index, (limit, _, _) in enumerate(BUDGET_TIERS) if limit is None or count <= limit)
    if lines >= LINES_BUMP:
        tier = min(tier + 1, len(BUDGET_TIERS) - 1)
    _, groups, rows = BUDGET_TIERS[tier]
    return {"max_groups": groups, "max_rows_per_group": rows,
            "max_edges": 0 if groups == 1 else min(MAX_DIAGRAM_EDGES, 2 * groups)}


def is_sensitive(path: str) -> bool:
    lowered = path.lower()
    return any(word in lowered for word in SENSITIVE_WORDS)


def _split_name(name: str) -> tuple[str, str]:
    stem, dot, extension = name.rpartition(".")
    return (stem, extension) if dot and stem else (name, "")


def classify(path: str) -> tuple[str, str]:
    """Return (language, category) from the path alone."""
    parts = [part for part in path.lower().split("/") if part]
    name = parts[-1] if parts else ""
    directories = set(parts[:-1])
    stem, extension = _split_name(name)
    language = LANGUAGES.get(extension, "other")
    if name in LOCK_FILES or name in EXTRA_LOCK_FILES or extension == "lock" or "-lock." in name:
        return language, "lockfile"
    if directories & VENDOR_DIRECTORIES:
        return language, "vendor"
    if (directories & GENERATED_DIRECTORIES or extension in {"snap", "map"} or ".min." in name
            or ".generated." in name or "_generated." in name or name.endswith(GENERATED_SUFFIXES)):
        return language, "generated"
    requirements = extension == "txt" and stem.startswith(("requirements", "constraints", "cmakelists"))
    if ((extension in DOC_EXTENSIONS and not requirements) or directories & DOC_DIRECTORIES
            or stem in DOC_NAMES or name in DOC_NAMES):
        return language, "docs"
    if extension in BINARY_EXTENSIONS:
        return language, "binary"
    if (directories & TEST_DIRECTORIES or ".test." in name or ".spec." in name or name.startswith("test_")
            or "_test." in name or "_spec." in name or name == "conftest.py"):
        return language, "test"
    if (requirements or extension in CONFIG_EXTENSIONS or name in CONFIG_NAMES or name.startswith(("dockerfile", "."))
            or ".config." in name or stem.endswith("config") or any(part.startswith(".") for part in directories)):
        return language, "config"
    return language, "source"


def _line_kind(value):
    return LINE_KINDS.get(value.lower() if isinstance(value, str) else None)


def _hunk_ranges(entry: dict) -> list[tuple[int, int, int]]:
    """New-side hunk ranges as (start, end, size) in validator terms; index order is hunk order."""
    ranges = []
    for hunk in entry.get("hunks") if isinstance(entry.get("hunks"), list) else []:
        if not isinstance(hunk, dict):
            continue
        start, size = _int(hunk.get("new_start")), _int(hunk.get("new_lines"))
        if start is not None and size is not None and start > 0 and size > 0:
            ranges.append((start, start + size - 1, size))
    return ranges


def _numbered_lines(hunk: dict):
    """Yield (kind, text, old_no, new_no) for each readable line, filling in missing numbers."""
    old, new = _int(hunk.get("old_start")), _int(hunk.get("new_start"))
    for line in hunk.get("lines") if isinstance(hunk.get("lines"), list) else []:
        kind = _line_kind(line.get("kind")) if isinstance(line, dict) else None
        if kind is None:
            continue
        text = line.get("text") if isinstance(line.get("text"), str) else ""
        old_no, new_no = _int(line.get("old_no")), _int(line.get("new_no"))
        if kind != "add":
            old_no = old if old_no is None else old_no
            old = None if old_no is None else old_no + 1
        else:
            old_no = None
        if kind != "del":
            new_no = new if new_no is None else new_no
            new = None if new_no is None else new_no + 1
        else:
            new_no = None
        yield kind, text, old_no, new_no


def _extract_refs(language: str, text: str) -> list[tuple]:
    """Import references on one source line: ("js", spec), ("py", level, module, names), ("go", path)."""
    if language in JS_LANGUAGES:
        stripped = text.lstrip()
        if stripped.startswith(("//", "/*", "*")) or not ("import" in text or "require" in text or "from" in text):
            return []
        found = []
        for pattern in (JS_FROM, JS_CLOSE, JS_SIDE_EFFECT):
            match = pattern.match(text)
            if match:
                found.append(match[1])
        found.extend(JS_CALL.findall(text))
        return [("js", spec) for spec in dict.fromkeys(found)]
    if language == "py":
        if "import" not in text or text.lstrip().startswith("#"):
            return []
        match = PY_FROM.match(text)
        if match:
            names = [name for name in (PY_NAME.match(part.strip().lstrip("(").strip())
                                       for part in text.split("#")[0].partition(" import ")[2].split(","))
                     if name]
            return [("py", len(match[1]), match[2] or "", tuple(dict.fromkeys(name[0] for name in names)))]
        match = PY_IMPORT.match(text.split("#")[0])
        if match:
            modules = [part.strip().split()[0] for part in match[1].split(",") if part.strip()]
            return [("py", 0, module, ()) for module in dict.fromkeys(modules) if re.fullmatch(r"[\w.]+", module)]
        return []
    if language == "go":
        if '"' not in text:
            return []
        match = GO_SINGLE.match(text) or GO_BLOCK_LINE.match(text)
        return [("go", match[1])] if match else []
    return []


def _scan_entry(entry: dict, language: str, imports: bool) -> dict:
    """Count changed lines and collect import references from one snapshot file entry."""
    additions = deletions = 0
    seen_lines = False
    refs = []
    for hunk in entry.get("hunks") if isinstance(entry.get("hunks"), list) else []:
        if not isinstance(hunk, dict):
            continue
        if isinstance(hunk.get("lines"), list):
            seen_lines = True
        for kind, text, old_no, new_no in _numbered_lines(hunk):
            additions += kind == "add"
            deletions += kind == "del"
            number, side = (old_no, "old") if kind == "del" else (new_no, "new")
            if imports and number is not None:
                refs.extend((side, number, ref) for ref in _extract_refs(language, text))
    if not seen_lines:
        # Hunks without line text: trust the file's own counts when it has them.
        additions, deletions = _int(entry.get("additions")) or 0, _int(entry.get("deletions")) or 0
    return {"additions": additions, "deletions": deletions, "refs": refs}


def _score(lines: int, sensitive: bool, penalty: float) -> float:
    return round(math.log1p(lines) + (SENSITIVE_BOOST if sensitive else 0.0) - penalty, 4)


def _unique_entries(snapshot) -> list[dict]:
    """Changed-file entries with a usable path; for a repeated path the last entry wins, in first place."""
    pr = snapshot.get("pr") if isinstance(snapshot, dict) else None
    files = pr.get("files") if isinstance(pr, dict) and isinstance(pr.get("files"), list) else []
    entries: dict[str, dict] = {}
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(path, str) and path:
            entries[path] = entry
    return list(entries.values())


class _Resolver:
    """Map an import reference in one changed file to other changed files."""

    def __init__(self, records: dict[str, dict]):
        self.records = records
        self._suffixes: dict[str, list[str]] | None = None
        self._go_dirs: dict[str, list[str]] | None = None

    def suffixes(self) -> dict[str, list[str]]:
        if self._suffixes is None:
            self._suffixes = {}
            for path in sorted(self.records):
                parts = path.split("/")
                for index in range(len(parts)):
                    self._suffixes.setdefault("/".join(parts[index:]), []).append(path)
        return self._suffixes

    def go_directories(self) -> dict[str, list[str]]:
        if self._go_dirs is None:
            self._go_dirs = {}
            for path in sorted(self.records):
                record = self.records[path]
                if record["language"] == "go" and not path.endswith("_test.go"):
                    self._go_dirs.setdefault(posixpath.dirname(path), []).append(path)
        return self._go_dirs

    def by_suffix(self, candidate: str) -> tuple[bool, str | None]:
        """`(matched, path)`: whether any changed file ends in `candidate`, and the path when only one does."""
        found = self.suffixes().get(candidate)
        return bool(found), (found[0] if found and len(found) == 1 else None)

    def javascript(self, source: str, spec: str) -> str | None:
        alias = spec.startswith(("@/", "~/"))
        if spec.startswith("."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(source), spec))
            if base == ".":
                base = ""
            if base == ".." or base.startswith("../"):
                return None
        elif alias:
            base = posixpath.normpath(spec[2:])
        else:
            return None
        candidates = [base] if base else []
        stripped = next((base[:-len(ext)] for ext in (".js", ".jsx", ".mjs", ".cjs") if base.endswith(ext)), None)
        if stripped:
            candidates += [stripped + ".ts", stripped + ".tsx"]
        candidates += [base + ext for ext in JS_EXTENSIONS if base]
        candidates += [f"{base}/index{ext}" if base else f"index{ext}" for ext in JS_EXTENSIONS]
        for candidate in candidates:
            if alias:
                matched, found = self.by_suffix(candidate)
                if matched:
                    return found  # None when several files match: ambiguous, so no edge
            elif candidate in self.records:
                return candidate
        return None

    def python(self, source: str, level: int, module: str, names: tuple) -> list[str]:
        modules = [module] if module else []
        modules += [f"{module}.{name}" if module else name for name in names]
        found = []
        for dotted in modules:
            relative = dotted.replace(".", "/")
            if level:
                base = source.split("/")[:-1]
                if level - 1 > len(base):
                    continue
                base = base[:len(base) - (level - 1)]
                relative = "/".join(base + [relative])
                options = [f"{relative}.py", f"{relative}/__init__.py"]
                target = next((option for option in options if option in self.records), None)
            else:
                target = None
                for option in (f"{relative}.py", f"{relative}/__init__.py"):
                    matched, target = self.by_suffix(option)
                    if matched:
                        break  # target is None when several files match: ambiguous, so no edge
            if target and target not in found:
                found.append(target)
        return found

    def go(self, source: str, spec: str, side: str = "new") -> tuple[str, list[str]] | None:
        """The changed Go package an import path names, as `(directory, its changed non-test .go files)`.

        An import names a package (a directory), never a file, so the caller may draw a file-level
        edge only when the package has exactly one changed file. On the new side a deleted file no
        longer belongs to the package.
        """
        first = spec.split("/", 1)[0]
        if "." not in first and first in GO_STD_ROOTS:
            return None
        parts = spec.split("/")
        directories = self.go_directories()
        for index in range(len(parts)):
            directory = "/".join(parts[index:])
            files = directories.get(directory)
            if files and directory != posixpath.dirname(source):
                if side == "new":
                    files = [path for path in files if self.records[path]["status"] != "deleted"]
                return (directory, files) if files else None
        return None


def _cluster(paths: list[str], limit: int) -> tuple[dict[tuple, list[str]], list[str], list[tuple]]:
    """Group paths by directory: split big groups by the next component, merge small ones upward.

    Returns (groups by directory tuple, overflow paths, directory tuples that went to overflow).
    """
    threshold = max(MIN_SPLIT_SIZE, -(-len(paths) // limit)) if paths else MIN_SPLIT_SIZE
    directories = {path: tuple(part for part in path.split("/")[:-1] if part) for path in paths}
    groups: dict[tuple, list[str]] = {}
    for path in paths:
        groups.setdefault(directories[path][:1], []).append(path)
    changed = True
    while changed:
        changed = False
        for key in sorted(groups):
            members = groups[key]
            if len(members) <= threshold:
                continue
            children: dict[tuple, list[str]] = {}
            for path in members:
                directory = directories[path]
                children.setdefault(directory[:len(key) + 1] if len(directory) > len(key) else key, []).append(path)
            if len(children) == 1 and key in children:
                continue
            del groups[key]
            for child, child_members in children.items():
                groups.setdefault(child, []).extend(child_members)
            changed = True
    overflow: list[str] = []
    overflow_keys: list[tuple] = []
    while len(groups) + bool(overflow) > limit:
        key = min(groups, key=lambda item: (len(groups[item]), -len(item), item))
        members = groups.pop(key)
        if len(key) <= 1:
            overflow += members
            overflow_keys.append(key)
        else:
            groups.setdefault(key[:-1], []).extend(members)
    return groups, overflow, overflow_keys


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-_")
    if not slug:
        return "group"
    return slug if slug[0].isalpha() else "g-" + slug


def _unique_id(base: str, taken: set[str]) -> str:
    base = base[:64].rstrip("-_") or "group"
    candidate, number = base, 2
    while candidate in taken:
        suffix = f"-{number}"
        candidate = base[:64 - len(suffix)] + suffix
        number += 1
    taken.add(candidate)
    return candidate


def _label(key: tuple) -> str:
    if not key:
        return ROOT_LABEL
    return _clean("/".join(key[-2:]), 80)


def _group_change(records: list[dict]) -> str:
    if records and all(record["status"] == "added" for record in records):
        return "added"
    if records and all(record["status"] == "deleted" for record in records):
        return "removed"
    return "modified"


def _dominant_penalty(records: list[dict]) -> float:
    lines: dict[str, int] = {}
    for record in records:
        lines[record["category"]] = lines.get(record["category"], 0) + record["additions"] + record["deletions"]
    if not lines:
        return 0.0
    category = min(lines, key=lambda item: (-lines[item], item))
    return CATEGORY_PENALTY.get(category, 0.0)


def build_skeleton(snapshot: dict, head: str) -> dict:
    """Build the deterministic skeleton described in the module docstring; never raises on odd shapes."""
    pr = snapshot.get("pr") if isinstance(snapshot, dict) and isinstance(snapshot.get("pr"), dict) else {}
    records: dict[str, dict] = {}
    refs: dict[str, list] = {}
    for entry in _unique_entries(snapshot):
        path = entry["path"]
        language, category = classify(path)
        routine = category in ROUTINE_CATEGORIES
        scanned = _scan_entry(entry, language, imports=not routine and language in JS_LANGUAGES | {"py", "go"})
        status = entry.get("status") if entry.get("status") in STATUSES else "modified"
        old_path = entry.get("old_path") if isinstance(entry.get("old_path"), str) and entry.get("old_path") else None
        sensitive = is_sensitive(path)
        records[path] = {
            "path": path, "old_path": old_path, "status": status,
            "additions": scanned["additions"], "deletions": scanned["deletions"],
            "language": language, "category": category, "sensitive": sensitive, "routine": routine,
            "lines_available": lines_available(entry), "has_new_lines": bool(_hunk_ranges(entry)),
            "score": _score(scanned["additions"] + scanned["deletions"], sensitive,
                            CATEGORY_PENALTY.get(category, 0.0)),
            "group": None,
        }
        refs[path] = scanned["refs"]
    kept = [record for record in records.values() if not record["routine"]]
    routine_files = [record for record in records.values() if record["routine"]]
    budget = size_budget(len(kept), sum(record["additions"] + record["deletions"] for record in kept))

    clustered, overflow, overflow_keys = _cluster([record["path"] for record in kept], budget["max_groups"])
    taken = {"root", "other"}
    ids = {}
    for key in sorted(clustered):
        ids[key] = "root" if not key else _unique_id(_slug("/".join(key)), taken)
    members = {key: list(paths) for key, paths in clustered.items()}
    labels = {key: _label(key) for key in clustered}
    paths = {key: "/".join(key) for key in clustered}
    if overflow:
        ids[None], members[None], labels[None], paths[None] = "other", overflow, OTHER_LABEL, ""

    def group_key(directory: tuple):
        """The group holding files of this directory: its key, None for overflow, False for none."""
        for length in range(len(directory), 0, -1):
            prefix = directory[:length]
            if prefix in clustered:
                return prefix
            if prefix in overflow_keys:
                return None
        if not directory:
            return () if () in clustered else (None if () in overflow_keys else False)
        return False

    routine_counts: dict = {}
    unassigned = 0
    for record in routine_files:
        key = group_key(tuple(part for part in record["path"].split("/")[:-1] if part))
        if key is False:
            unassigned += 1
            continue
        routine_counts[key] = routine_counts.get(key, 0) + 1
        record["group"] = ids[key]

    groups = []
    for key, group_paths in members.items():
        group_records = sorted((records[path] for path in group_paths), key=lambda record: (-record["score"], record["path"]))
        for record in group_records:
            record["group"] = ids[key]
        additions = sum(record["additions"] for record in group_records)
        deletions = sum(record["deletions"] for record in group_records)
        sensitive = any(record["sensitive"] for record in group_records)
        groups.append({
            "id": ids[key], "label": labels[key], "path": paths[key],
            "files": [record["path"] for record in group_records],
            "routine_count": routine_counts.get(key, 0),
            "additions": additions, "deletions": deletions, "sensitive": sensitive,
            "change": _group_change(group_records),
            "score": _score(additions + deletions, sensitive, _dominant_penalty(group_records)),
            "citable_files": sum(record["has_new_lines"] for record in group_records),
        })
    groups.sort(key=lambda group: (-group["score"], group["path"], group["id"]))

    edges = _import_edges(records, refs)
    ordered = sorted(records.values(), key=lambda record: record["path"])
    for edge in edges:
        edge["source_group"] = records[edge["source_file"]]["group"]
        edge["target_group"] = records[edge["target_file"] or edge["target_anchor"]]["group"]
    return {
        "head": head if isinstance(head, str) else "",
        "head_bound": snapshot_binds_head(pr, head) if isinstance(head, str) else False,
        "totals": {
            "files": len(records),
            "additions": sum(record["additions"] for record in records.values()),
            "deletions": sum(record["deletions"] for record in records.values()),
            "routine_files": len(routine_files),
        },
        "files": ordered,
        "groups": groups,
        "routine_unassigned": unassigned,
        "edges": edges,
        "budget": budget,
        "docs_only": not records or (
            bool(kept) and all(record["category"] == "docs" for record in kept)),
        "no_citable_lines": not any(record["has_new_lines"] for record in records.values()),
    }


def _import_edges(records: dict[str, dict], refs: dict[str, list]) -> list[dict]:
    """Resolve each import line to another changed, non-routine file; sorted, deduplicated, capped.

    A target is resolved to a file only when that is unambiguous. A Go import names a package, so
    it gives a file edge only when the package has exactly one changed file; otherwise the edge
    is package-level: `target_file` is None, `target_package` is the directory and `target_anchor`
    is one changed file of that package (used only to find its group). Ambiguous Python and
    alias suffix matches produce no edge at all.
    """
    resolver = _Resolver({path: record for path, record in records.items() if not record["routine"]})
    found = set()
    for source in sorted(refs):
        for side, line, ref in refs[source]:
            if ref[0] == "go":
                package = resolver.go(source, ref[1], side)
                if package is None:
                    continue
                directory, files = package
                if len(files) == 1:
                    found.add((source, line, side, files[0], None, None))
                else:
                    found.add((source, line, side, None, directory, files[0]))
                continue
            if ref[0] == "js":
                targets = [resolver.javascript(source, ref[1])]
            else:
                targets = resolver.python(source, ref[1], ref[2], ref[3])
            for target in targets:
                if target and target != source and (side == "old" or resolver.records[target]["status"] != "deleted"):
                    found.add((source, line, side, target, None, None))
    ordered = sorted(found, key=lambda item: (item[0], item[1], item[2], item[3] or "", item[4] or ""))[:MAX_SKELETON_EDGES]
    edges = []
    for source, line, side, target, package, anchor in ordered:
        edge = {"source_file": source, "target_file": target, "source_group": None, "target_group": None,
                "cite": {"path": source, "line": line, "side": side}, "kind": "import"}
        if package is not None:
            edge.update(target_package=package, target_anchor=anchor)
        edges.append(edge)
    return edges


# ---- Deterministic ADE v1 diagram -------------------------------------------------------------

KIND_WORDS = (
    ("database", {"db", "database", "databases", "migration", "migrations", "schema", "schemas", "sql", "prisma"}),
    ("worker", {"queue", "queues", "job", "jobs", "worker", "workers"}),
    ("client", {"web", "ui", "components", "component", "pages", "page", "app", "frontend", "client", "views"}),
)


def _node_kind(path: str, records: list[dict]) -> str:
    tokens = {token for token in re.split(r"[^a-z0-9]+", path.lower()) if token}
    for kind, words in KIND_WORDS:
        if tokens & words:
            return kind
    if records and all(record["language"] == "sql" for record in records):
        return "database"
    return "service"


def _file_hunks(entry: dict) -> list[tuple[int, int, int]]:
    """Citable (size, start, end) for each new-side hunk, biggest first; ranges narrow to added lines.

    Every returned range lies inside the hunk's own new-side range, which is what the validator checks.
    """
    result = []
    hunks = entry.get("hunks") if isinstance(entry.get("hunks"), list) else []
    for hunk in hunks:
        if not isinstance(hunk, dict):
            continue
        start, size = _int(hunk.get("new_start")), _int(hunk.get("new_lines"))
        if start is None or size is None or start <= 0 or size <= 0:
            continue
        end = start + size - 1
        added = [new_no for kind, _, _, new_no in _numbered_lines(hunk) if kind == "add" and new_no is not None]
        if added and start <= min(added) and max(added) <= end:
            first, last = min(added), max(added)
        else:
            first, last = start, end
        result.append((size, first, last))
    return sorted(result, key=lambda item: (-item[0], item[1], item[2]))


def _layers(order: list[str], pairs: set[tuple[str, str]]) -> dict[str, int]:
    """Longest path from importers (layer 0) to what they import, with back edges dropped in DFS order."""
    index = {name: position for position, name in enumerate(order)}
    adjacent = {name: sorted((target for source, target in pairs if source == name), key=index.get) for name in order}
    state: dict[str, int] = {}
    finished: list[str] = []
    kept: dict[str, list[str]] = {name: [] for name in order}
    for root in order:
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(adjacent[root]))]
        while stack:
            node, targets = stack[-1]
            advanced = False
            for target in targets:
                if state.get(target) == 1:
                    continue  # closes a cycle: dropped
                kept[node].append(target)
                if target not in state:
                    state[target] = 1
                    stack.append((target, iter(adjacent[target])))
                    advanced = True
                    break
            if not advanced:
                state[node] = 2
                finished.append(node)
                stack.pop()
    layers = {name: 0 for name in order}
    for node in reversed(finished):
        for target in kept[node]:
            layers[target] = max(layers[target], layers[node] + 1)
    return {name: min(layer, MAX_LAYER) for name, layer in layers.items()}


def _merge_groups(groups: list[dict], records: dict[str, dict], taken: set[str]) -> dict:
    """Combine groups into one synthetic "Other changes" group."""
    paths = sorted((path for group in groups for path in group["files"]),
                   key=lambda path: (-records[path]["score"], path))
    members = [records[path] for path in paths]
    additions, deletions = sum(group["additions"] for group in groups), sum(group["deletions"] for group in groups)
    return {
        "id": _unique_id("other", taken), "label": OTHER_LABEL, "path": "", "files": paths,
        "routine_count": sum(group["routine_count"] for group in groups),
        "additions": additions, "deletions": deletions,
        "sensitive": any(group["sensitive"] for group in groups), "change": _group_change(members),
        "score": 0.0, "citable_files": sum(group["citable_files"] for group in groups),
    }


def _cap_gap(text: str) -> str:
    return _clean(text, MAX_GAP_CHARS)


def deterministic_architecture(skeleton: dict, head: str, snapshot: dict) -> dict | None:
    """Build an ADE v1 document from the skeleton, or None when there is nothing citable to draw.

    Nodes are the best-ranked groups with new-side hunk lines (extra groups merge into one "Other
    changes" node), edges are new-side imports between nodes, and every citation is a real new-side
    hunk range or import line from the snapshot. The caller still runs validate_architecture.
    """
    if not isinstance(skeleton, dict) or skeleton.get("docs_only") or skeleton.get("no_citable_lines"):
        return None
    pr = snapshot.get("pr") if isinstance(snapshot, dict) and isinstance(snapshot.get("pr"), dict) else {}
    entries = {entry["path"]: entry for entry in _unique_entries(snapshot)}
    records = {record["path"]: record for record in skeleton.get("files") or []}
    budget = skeleton.get("budget") or size_budget(0, 0)

    hunk_cache: dict[str, list] = {}

    def hunks_of(path: str) -> list:
        if path not in hunk_cache:
            hunk_cache[path] = _file_hunks(entries[path]) if path in entries else []
        return hunk_cache[path]

    def citable(path: str) -> bool:
        record = records.get(path)
        return bool(record and record["has_new_lines"] and len(path) <= MAX_PATH_CHARS and hunks_of(path))

    eligible, without_lines = [], 0
    for group in skeleton.get("groups") or []:
        if any(citable(path) for path in group["files"]):
            eligible.append(group)
        else:
            without_lines += 1
    if not eligible:
        return None
    limit = max(1, min(MAX_NODES, budget["max_groups"]))
    merged_count = 0
    if len(eligible) > limit:
        taken = {group["id"] for group in skeleton["groups"]}
        rest = eligible[limit - 1:]
        merged_count = len(rest)
        eligible = eligible[:limit - 1] + [_merge_groups(rest, records, taken)]
    node_for_file = {path: group["id"] for group in eligible for path in group["files"]}

    rows = max(1, min(MAX_NODE_FILES, budget["max_rows_per_group"]))
    nodes = []
    for group in eligible:
        listed = [path for path in group["files"] if citable(path)][:rows]
        hunks = {path: hunks_of(path) for path in listed}
        picks = [(position, hunks[path][0]) for position, path in enumerate(listed)]
        depth = 1
        while len(picks) < MAX_EVIDENCE and depth < max((len(item) for item in hunks.values()), default=0):
            for position, path in enumerate(listed):
                if len(picks) < MAX_EVIDENCE and len(hunks[path]) > depth:
                    picks.append((position, hunks[path][depth]))
            depth += 1
        evidence = [{"type": "diff", "path": listed[position], "start": first, "end": last}
                    for position, (_, first, last) in sorted(picks, key=lambda item: (item[0], item[1][1], item[1][2]))]
        evidence = [dict(item) for item in {tuple(item.items()): item for item in evidence}.values()]
        count = len(group["files"])
        names = ", ".join(_clean(posixpath.basename(path), 60) for path in listed[:5])
        more = f" and {count - min(len(listed), 5)} more" if count > min(len(listed), 5) else ""
        description = f"{_plural(count, 'file')} changed (+{group['additions']}/-{group['deletions']}): {names}{more}."
        if group["routine_count"]:
            description += f" {_plural(group['routine_count'], 'routine file')} collapsed."
        nodes.append({
            "id": group["id"], "label": _clean(group["label"], 80) or "Changes",
            "kind": _node_kind(group["path"], [records[path] for path in group["files"]]),
            "layer": 0, "change": group["change"], "description": description,
            "files": listed, "episode_ids": [], "evidence": evidence,
        })

    ranges = {path: [(start, end) for start, end, _ in _hunk_ranges(entries[path])] for path in node_for_file if path in entries}
    aggregated: dict[tuple[str, str], list[dict]] = {}
    for edge in skeleton.get("edges") or []:
        cite = edge["cite"]
        if cite["side"] != "new":
            continue
        source = node_for_file.get(edge["source_file"])
        target = node_for_file.get(edge["target_file"] or edge["target_anchor"])
        path, line = cite["path"], cite["line"]
        if (not source or not target or source == target or len(path) > MAX_PATH_CHARS
                or not any(start <= line <= end for start, end in ranges.get(path, []))):
            continue
        aggregated.setdefault((source, target), []).append(edge)
    max_edges = max(0, min(MAX_DIAGRAM_EDGES, budget["max_edges"]))
    chosen = sorted(aggregated.items(), key=lambda item: (-len(item[1]), item[0]))[:max_edges]
    chosen.sort(key=lambda item: item[0])
    change_of = {node["id"]: node["change"] for node in nodes}
    edges = []
    for number, ((source, target), items) in enumerate(chosen, start=1):
        items = sorted(items, key=lambda item: (item["cite"]["path"], item["cite"]["line"], item["target_file"] or ""))
        first = items[0]
        # A package-level import (Go) names the package directory, never one of its files.
        named = (f"the {_clean(first['target_package'], 120)} package" if first["target_file"] is None
                 else _clean(posixpath.basename(first["target_file"]), 80))
        text = f"{_clean(posixpath.basename(first['source_file']), 80)} imports {named}"
        if len(items) > 1:
            text += f" and {len(items) - 1} more {'import' if len(items) == 2 else 'imports'}"
        edges.append({
            "id": f"e{number}", "source": source, "target": target, "label": "imports",
            "change": "added" if change_of[source] == change_of[target] == "added" else "modified",
            "description": text, "episode_ids": [],
            "evidence": [{"type": "diff", "path": item["cite"]["path"], "start": item["cite"]["line"],
                          "end": item["cite"]["line"]} for item in items[:MAX_EVIDENCE]],
        })
    layers = _layers([node["id"] for node in nodes], {(edge["source"], edge["target"]) for edge in edges})
    for node in nodes:
        node["layer"] = layers[node["id"]]

    totals = skeleton.get("totals") or {}
    shown_files = sum(len(node["files"]) for node in nodes)
    routine = totals.get("routine_files", 0)
    unlisted = max(0, sum(len(group["files"]) for group in eligible) - shown_files)
    no_data = sum(1 for record in records.values() if not record["routine"] and not record["lines_available"])
    gaps = []
    if routine:
        gaps.append(f"{_plural(routine, 'routine file')} (lockfiles, generated, vendored or binary) collapsed, not shown.")
    if no_data:
        gaps.append(f"{_plural(no_data, 'file')} had no line data and cannot be cited.")
    if without_lines:
        gaps.append(f"{_plural(without_lines, 'area')} with no new lines to cite (for example deleted files) are left out.")
    if merged_count:
        gaps.append(f"{_plural(merged_count, 'smaller area')} merged into Other changes to fit the size limit.")
    if unlisted:
        gaps.append(f"{_plural(unlisted, 'changed file')} not listed individually because of the row limit.")
    gaps = gaps[:MAX_GAPS - 1]
    gaps.append("Generated deterministically from file structure and imports, not from reading the code.")
    changed = totals.get("files", len(records))
    title = _clean((pr.get("title") if isinstance(pr.get("title"), str) else ""), 160) or "Pull request changes"
    return {
        "schema_version": 1, "head_sha": head, "title": title,
        "description": (f"Changes {_plural(changed, 'file')} in {_plural(len(skeleton.get('groups') or []), 'area')} "
                        f"(+{totals.get('additions', 0)}/-{totals.get('deletions', 0)})."),
        "nodes": nodes, "edges": edges, "flows": [], "gaps": [_cap_gap(gap) for gap in gaps],
    }


def skeleton_stats(skeleton: dict) -> dict:
    """The counts the status file records."""
    return {"files": len(skeleton.get("files") or []), "groups": len(skeleton.get("groups") or []),
            "edges": len(skeleton.get("edges") or []),
            "routine_files": (skeleton.get("totals") or {}).get("routine_files", 0)}
