"""Pattern rows and API surface counts for the schema-v2 architecture diagram.

Pure functions over snapshot file entries (`{"path", "status", "hunks": [{"lines": [{"kind", "text"}]}]}`):
no model, no I/O, stdlib only, and the same input always gives the same output. Nothing here raises on
odd shapes; unreadable input counts as "no changed lines".

  change_signature(file)            16-hex fingerprint of a file's change, or None
  cluster_same_change(files, ...)   files that share one signature, 3 or more at a time
  category_members(files, cat, ...) the tests / docs / config files of a group
  verify_pattern(pattern, by_path)  structured errors for a node's `pattern` object (validator use)
  pattern_label(family, count, ...) the short row label
  api_surface(file), api_total(fs)  lexical counts of exported names added and removed

Change signature: sha256 over the file status and the sorted sets of its NET added and NET deleted lines (how
often a line repeats is ignored). Each changed line is trimmed and loses a trailing line comment; lines that are
empty or pure comments after that are ignored, as are context lines. The net change is `added - deleted` and
`deleted - added` as sets, so a line edited only in its comment (delete `x();`, add `x(); // reviewed`) cancels out,
and the same comment appended to different lines of different files gives the same signature. Only then does every
run of ASCII digits in the remaining lines become `#` (so a version bump is still a change). Comments are
recognised by file extension: `//` for ts/js/go/java/c/rs and similar, `#` for py/sh/rb/yaml/toml/Makefile, `--` for
sql/lua, plus `/* ... */` and `*` lines in the `//` languages. A marker counts only at the start of the line or after
whitespace and outside a quoted span, so `https://x` and `"a // b"` stay intact; unknown extensions are not
stripped. A file with no net changed line (binary, no hunks, whitespace-only or comment-only edits) has no signature.

API surface (see docs/architecture-v2-contract.md): lexical, one language per file extension.
  TypeScript/JavaScript  `export` declarations (function, class, const, let, var, type, interface, enum,
                         namespace, abstract/async/declare forms), `export default` (name "default"),
                         `export { a, b as c }` (the exported name, so "c"; `type` modifiers dropped;
                         a block opened on a changed line may continue over the next lines of that side),
                         `export * from` (name "*"; `export * as ns from` is "ns").
  Python                 top-level (column 0) `def`, `async def`, `class` whose name has no leading "_".
  Go                     top-level `func Name(`, methods `func (r T) Name(` (named "T.Name"), `type`,
                         `var`, `const` with an uppercase name, including `var A, B = ...`, plus names at
                         one indent level inside a `var (`/`const (`/`type (` block whose opening line is
                         in the hunk. A block that opens before the hunk is not seen.
Names are counted once per side per file, in order of appearance. A name removed and re-added in one
file counts in both `removed` and `added`. `added_names` / `removed_names` keep the first 20.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from paircli_skeleton import _line_kind, classify

SIGNATURE_CHARS = 16
FAMILIES = ("same-change", "tests", "docs", "config", "remainder")
CATEGORY_FAMILIES = {"tests": "test", "docs": "docs", "config": "config"}  # family -> skeleton category
MIN_MEMBERS = 3
MAX_MEMBERS = 500
MAX_API_NAMES = 20
MAX_VERIFY_ERRORS = 100
MAX_LABEL_CHARS = 80
MAX_BLOCK_LINES = 100

DIGITS = re.compile(r"[0-9]+")
STATUS_WORDS = {"deleted": "deleted", "added": "added", "renamed": "renamed and edited", "modified": "same edit"}

JS_LANGUAGES = {"ts", "tsx", "js", "jsx"}
JS_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
JS_DEFAULT = re.compile(r"^\s*export\s+default\b")
JS_DECL = re.compile(
    r"^\s*export\s+(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:(?:const\s+)?enum|function\s*\*?|class|const|let|var|type|interface|namespace|module)\s+([A-Za-z_$][\w$]*)")
JS_STAR = re.compile(r"^\s*export\s+\*\s+(?:as\s+([A-Za-z_$][\w$]*)\s+)?from\b")
JS_BRACE = re.compile(r"^\s*export\s+(?:type\s+)?\{(.*)$")
PY_DEF = re.compile(r"^(?:async\s+def|def|class)\s+([^\W\d]\w*)")
GO_FUNC = re.compile(r"^func\s+([A-Z]\w*)\s*[\[(]")
GO_METHOD = re.compile(r"^func\s*\(([^)]*)\)\s*([A-Z]\w*)\s*[\[(]")
GO_TYPE = re.compile(r"^type\s+([A-Z]\w*)")
GO_VALUE = re.compile(r"^(?:var|const)\s+(\w+(?:\s*,\s*\w+)*)")
GO_BLOCK_OPEN = re.compile(r"^(?:var|const|type)\s*\(\s*(?://.*)?$")
GO_BLOCK_CLOSE = re.compile(r"^\)")
GO_BLOCK_NAME = re.compile(r"^(?:\t| {4})([A-Z]\w*(?:\s*,\s*[A-Z]\w*)*)(?!\w)(?!\s*:)")


def _int(value):
    return value if type(value) is int else None


def _changed_lines(file):
    """Yield ("add" | "del", text) for each added or deleted line of a snapshot file entry."""
    for hunk in _hunks(file):
        for kind, text, _ in _hunk_lines(hunk):
            if kind != "ctx":
                yield kind, text


def _hunks(file):
    hunks = file.get("hunks") if isinstance(file, dict) else None
    return [hunk for hunk in hunks if isinstance(hunk, dict)] if isinstance(hunks, list) else []


def _hunk_lines(hunk):
    """Yield (kind, text, line) for each readable line of a hunk; kind is add, del or ctx."""
    lines = hunk.get("lines")
    for line in lines if isinstance(lines, list) else []:
        kind = _line_kind(line.get("kind")) if isinstance(line, dict) else None
        if kind is not None:
            text = line.get("text")
            yield kind, text if isinstance(text, str) else "", line


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _path(file) -> str:
    return _text(file.get("path")) if isinstance(file, dict) else ""


# --- change signature ---------------------------------------------------------------------------

SLASH_LANGUAGES = frozenset("ts tsx js jsx mjs cjs go java c h cc cpp cxx hpp rs kt kts swift cs php scala dart".split())
HASH_LANGUAGES = frozenset("py sh bash zsh rb yaml yml toml pl r mk dockerfile".split())
DASH_LANGUAGES = frozenset({"sql", "lua"})
HASH_NAMES = frozenset({"makefile", "gnumakefile", "dockerfile"})
QUOTES = "\"'`"


def _comment_marker(path: str) -> str | None:
    """The line-comment marker of a file's language by extension, or None when its comments are unknown."""
    name = path.rsplit("/", 1)[-1].lower()
    extension = name.rsplit(".", 1)[-1] if "." in name else ""
    if extension in SLASH_LANGUAGES:
        return "//"
    if extension in HASH_LANGUAGES or name in HASH_NAMES:
        return "#"
    if extension in DASH_LANGUAGES:
        return "--"
    return None


def _strip_comment(text: str, marker: str) -> str:
    """`text` (already trimmed) without a trailing line comment; unchanged when unsure.

    The marker must sit at the start or after whitespace and outside a quoted span (an unclosed quote
    keeps the rest of the line, so an odd line is never cut). C-like languages also lose a
    trailing `/* ... */`, and a line that is only a block-comment line (`/*`, `*`, `*/`) is emptied.
    """
    c_like = marker == "//"
    if c_like and (text == "*" or text.startswith(("*/", "* ", "*\t")) or (
            text.startswith("/*") and ("*/" not in text or text.endswith("*/")))):
        return ""
    quote, index, size = "", 0, len(text)
    while index < size:
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in QUOTES:
            quote = char
        elif (text.startswith(marker, index) or (c_like and text.startswith("/*", index) and text.endswith("*/"))) \
                and (index == 0 or text[index - 1].isspace()):
            return text[:index].rstrip()
        index += 1
    return text


def _net_lines(file) -> tuple[list[str], list[str]]:
    """The sorted net added and net deleted lines of a file, without comment noise and digit-normalised.

    The net sets are taken before digits become `#`, so a line changed only in a digit (a version bump)
    still counts as a change, while one changed only in its comment cancels out.
    """
    marker = _comment_marker(_path(file))
    added: set[str] = set()
    deleted: set[str] = set()
    for kind, text in _changed_lines(file):
        text = text.strip()
        if marker and text:
            text = _strip_comment(text, marker)
        if text:
            (added if kind == "add" else deleted).add(text)
    return (sorted({DIGITS.sub("#", text) for text in added - deleted}),
            sorted({DIGITS.sub("#", text) for text in deleted - added}))


def change_signature(file) -> str | None:
    """Return the 16-hex change signature of a snapshot file entry, or None without a net changed line."""
    try:
        added, deleted = _net_lines(file)
        if not added and not deleted:
            return None
        payload = json.dumps([_text(file.get("status")), added, deleted], ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:SIGNATURE_CHARS]
    except Exception:  # noqa: BLE001 - odd input must never break a diagram
        return None


def _line_counts(file) -> tuple[int, int]:
    added = deleted = 0
    for kind, _ in _changed_lines(file):
        added += kind == "add"
        deleted += kind == "del"
    return added, deleted


# --- clustering ---------------------------------------------------------------------------------

def _balanced_chunks(paths: list[str], limit: int) -> list[list[str]]:
    count = math.ceil(len(paths) / limit)
    size, extra = divmod(len(paths), count)
    chunks, start = [], 0
    for index in range(count):
        end = start + size + (1 if index < extra else 0)
        chunks.append(paths[start:end])
        start = end
    return chunks


def _split_members(paths: list[str], limit: int, depth: int = 0) -> list[list[str]]:
    """Split sorted `paths` into groups of at most `limit`, along directories where possible."""
    if len(paths) <= limit:
        return [paths]
    groups: dict[str, list[str]] = {}
    for path in paths:
        parts = path.split("/")
        groups.setdefault(parts[depth] if depth < len(parts) - 1 else "", []).append(path)
    if len(groups) == 1:
        # One bucket: go a level deeper while directories remain, else cut sorted chunks.
        if next(iter(groups)):
            return _split_members(paths, limit, depth + 1)
        return _balanced_chunks(paths, limit)
    result, pending = [], []
    for key in sorted(groups):
        group = groups[key]
        if len(group) > limit:
            result.extend(_split_members(group, limit, depth + 1) if key else _balanced_chunks(group, limit))
        elif len(pending) + len(group) > limit:
            result.append(pending)
            pending = list(group)
        else:
            pending.extend(group)
    if pending:
        result.append(pending)
    return result


def cluster_same_change(files, *, min_members: int = MIN_MEMBERS, max_members: int = MAX_MEMBERS,
                        skip=None) -> list[dict]:
    """Group files with the same change signature; each file lands in at most one cluster.

    Returns [{"signature", "status", "members" (sorted paths), "additions", "deletions"}], largest
    first, then by signature and first member. A signature shared by more than `max_members` files
    is split by directory (small directories packed together) and, where one directory is still too
    large, into balanced sorted chunks; every piece keeps the signature. Pieces below `min_members`
    are dropped. Paths in `skip` are ignored; a repeated path counts once (first entry wins).
    """
    try:
        minimum = max(1, _int(min_members) or MIN_MEMBERS)
        maximum = max(minimum, _int(max_members) or MAX_MEMBERS)
        skipped = {item for item in skip if isinstance(item, str)} if skip else set()
        seen: set[str] = set()
        by_signature: dict[str, dict] = {}
        for file in files if isinstance(files, (list, tuple)) else []:
            path = _path(file)
            if not path or path in seen or path in skipped:
                continue
            seen.add(path)
            signature = change_signature(file)
            if signature is None:
                continue
            added, deleted = _line_counts(file)
            bucket = by_signature.setdefault(signature, {"members": {}, "status": _text(file.get("status"))})
            bucket["members"][path] = (added, deleted)
        clusters = []
        for signature, bucket in by_signature.items():
            if len(bucket["members"]) < minimum:
                continue
            for members in _split_members(sorted(bucket["members"]), maximum):
                if len(members) < minimum:
                    continue
                clusters.append({
                    "signature": signature, "status": bucket["status"], "members": members,
                    "additions": sum(bucket["members"][path][0] for path in members),
                    "deletions": sum(bucket["members"][path][1] for path in members),
                })
        clusters.sort(key=lambda cluster: (-len(cluster["members"]), cluster["signature"], cluster["members"][0]))
        return clusters
    except Exception:  # noqa: BLE001
        return []


def category_members(files, category: str, *, min_members: int = MIN_MEMBERS, skip=None) -> list[str]:
    """Sorted paths of `files` in category "tests", "docs" or "config"; [] with fewer than `min_members`."""
    wanted = CATEGORY_FAMILIES.get(category) if isinstance(category, str) else None
    if wanted is None:
        return []
    try:
        skipped = {item for item in skip if isinstance(item, str)} if skip else set()
        paths = {path for path in (_path(file) for file in files) if path and path not in skipped
                 and classify(path)[1] == wanted} if isinstance(files, (list, tuple)) else set()
    except Exception:  # noqa: BLE001
        return []
    return sorted(paths) if len(paths) >= max(1, _int(min_members) or MIN_MEMBERS) else []


# --- pattern verification -----------------------------------------------------------------------

def verify_pattern(pattern, files_by_path) -> list[dict]:
    """Return [{"path": "pattern.members[3]", "reason": ...}] for every problem; [] means valid.

    `files_by_path` maps each changed path to its snapshot entry. At most 100 errors are returned.
    """
    errors: list[dict] = []

    def fail(path: str, reason: str) -> None:
        if len(errors) < MAX_VERIFY_ERRORS:
            errors.append({"path": path, "reason": reason})

    if not isinstance(pattern, dict):
        return [{"path": "pattern", "reason": "pattern must be an object"}]
    by_path = files_by_path if isinstance(files_by_path, dict) else {}
    family = pattern.get("family")
    if family not in FAMILIES:
        fail("pattern.family", f"family must be one of {', '.join(FAMILIES)}")
    members = pattern.get("members")
    if not isinstance(members, list):
        fail("pattern.members", "members must be a list of paths")
        return errors
    if not MIN_MEMBERS <= len(members) <= MAX_MEMBERS:
        fail("pattern.members", f"members must hold {MIN_MEMBERS} to {MAX_MEMBERS} paths, got {len(members)}")
    count = pattern.get("count")
    if type(count) is not int or count != len(members):
        fail("pattern.count", f"count must equal the number of members ({len(members)})")
    signature = pattern.get("signature")
    if family == "same-change" and (not isinstance(signature, str) or not signature):
        fail("pattern.signature", "same-change needs a signature")
    if family == "remainder" and "signature" in pattern:
        fail("pattern.signature", "a remainder pattern has no signature")
    seen: set[str] = set()
    for index, member in enumerate(members):
        where = f"pattern.members[{index}]"
        if not isinstance(member, str) or not member:
            fail(where, "member must be a non-empty path string")
            continue
        if member in seen:
            fail(where, f"duplicate member {member}")
            continue
        seen.add(member)
        file = by_path.get(member)
        if not isinstance(file, dict):
            fail(where, f"{member} is not a changed file")
            continue
        if family == "same-change" and isinstance(signature, str) and signature:
            actual = change_signature(file)
            if actual is None:
                fail(where, f"{member} has no net changed line (comments ignored), so no change signature")
            elif actual != signature:
                fail(where, f"{member} has change signature {actual}, not {signature}")
        elif family in CATEGORY_FAMILIES and classify(member)[1] != CATEGORY_FAMILIES[family]:
            fail(where, f"{member} is not a {family} file")
    return errors


def pattern_label(family, count, status=None) -> str:
    """Short deterministic row label, at most 80 characters."""
    number = _int(count) if _int(count) is not None else 0
    files = f"{number} file" + ("" if number == 1 else "s")
    if family == "same-change":
        word = STATUS_WORDS.get(status.lower(), "same change") if isinstance(status, str) else "same change"
        label = f"{files}: {word}"
    elif family == "tests":
        label = f"{number} test file" + ("" if number == 1 else "s")
    elif family == "docs":
        label = f"{number} doc file" + ("" if number == 1 else "s")
    elif family == "config":
        label = f"{number} config file" + ("" if number == 1 else "s")
    elif family == "remainder":
        label = f"{number} other changed file" + ("" if number == 1 else "s")
    else:
        label = files
    return label[:MAX_LABEL_CHARS]


# --- API surface --------------------------------------------------------------------------------

def _side_lines(hunk: dict, side: str):
    """Yield (text, changed) for the lines of one side of a hunk: its context plus its own changes."""
    for kind, text, _ in _hunk_lines(hunk):
        if kind == "ctx":
            yield text, False
        elif kind == side:
            yield text, True


def _brace_names(body: str) -> list[str]:
    names = []
    for item in body.split(","):
        item = re.sub(r"//.*$", "", item).strip()
        item = re.sub(r"^type\s+(?=\S)", "", item)
        name = item.split()[-1] if " as " in f" {item} " else item
        if JS_IDENT.fullmatch(name):
            names.append(name)
    return names


def _scan_js(stream) -> list[str]:
    names, buffer, buffered = [], None, 0
    for text, changed in stream:
        if buffer is not None:
            buffer.append(re.sub(r"//.*$", "", text))
            buffered += 1
            joined = " ".join(buffer)
            if "}" in joined or buffered > MAX_BLOCK_LINES:
                if "}" in joined:
                    names.extend(_brace_names(joined.split("}", 1)[0]))
                buffer = None
            continue
        if not changed or "export" not in text:
            continue
        if JS_DEFAULT.match(text):
            names.append("default")
            continue
        match = JS_STAR.match(text)
        if match:
            names.append(match[1] or "*")
            continue
        match = JS_BRACE.match(text)
        if match:
            if "}" in match[1]:
                names.extend(_brace_names(match[1].split("}", 1)[0]))
            else:
                buffer, buffered = [re.sub(r"//.*$", "", match[1])], 0
            continue
        match = JS_DECL.match(text)
        if match:
            names.append(match[1])
    return names


def _scan_py(stream) -> list[str]:
    names = []
    for text, changed in stream:
        match = PY_DEF.match(text) if changed else None
        if match and not match[1].startswith("_"):
            names.append(match[1])
    return names


def _go_receiver(receiver: str) -> str:
    """The type name of a Go receiver such as `r *Repo[K]`."""
    parts = re.sub(r"\[.*\]", "", receiver).split()
    return parts[-1].lstrip("*") if parts else "?"


def _scan_go(stream) -> list[str]:
    names, in_block = [], False
    for text, changed in stream:
        if in_block:
            if GO_BLOCK_CLOSE.match(text):
                in_block = False
            elif changed:
                match = GO_BLOCK_NAME.match(text)
                if match:
                    names.extend(name.strip() for name in match[1].split(","))
            continue
        if GO_BLOCK_OPEN.match(text):
            in_block = True
            continue
        if not changed:
            continue
        match = GO_FUNC.match(text)
        if match:
            names.append(match[1])
            continue
        match = GO_METHOD.match(text)
        if match:
            names.append(f"{_go_receiver(match[1])}.{match[2]}")
            continue
        match = GO_TYPE.match(text)
        if match:
            names.append(match[1])
            continue
        match = GO_VALUE.match(text)
        if match:
            names.extend(name for name in re.split(r"\s*,\s*", match[1]) if name[:1].isupper() and name[:1].isascii())
    return names


SCANNERS = {"ts": _scan_js, "tsx": _scan_js, "js": _scan_js, "jsx": _scan_js, "py": _scan_py, "go": _scan_go}


def _surface(file, side: str) -> list[str]:
    scanner = SCANNERS.get(classify(_path(file))[0])
    if scanner is None:
        return []
    names: dict[str, None] = {}
    for hunk in _hunks(file):
        for name in scanner(_side_lines(hunk, side)):
            names[name] = None
    return list(names)


def api_surface(file) -> dict:
    """Exported names a file adds (from added lines) and removes (from deleted lines)."""
    try:
        added, removed = _surface(file, "add"), _surface(file, "del")
    except Exception:  # noqa: BLE001
        added = removed = []
    return {"added": len(added), "removed": len(removed),
            "added_names": added[:MAX_API_NAMES], "removed_names": removed[:MAX_API_NAMES]}


def api_total(files) -> dict:
    """Sum of api_surface over files: {"added": int, "removed": int}."""
    added = removed = 0
    for file in files if isinstance(files, (list, tuple)) else []:
        surface = api_surface(file)
        added += surface["added"]
        removed += surface["removed"]
    return {"added": added, "removed": removed}
