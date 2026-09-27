import re
from dataclasses import dataclass, field

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError


@dataclass
class Hunk:
    target_start: int
    source_start: int
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)
    target_len: int = 0
    source_len: int = 0

    def covers(self, side: str, line: int) -> bool:
        """Whether GitHub accepts a review comment on this line (any line shown in the hunk)."""
        if side == "additions":
            return self.target_start <= line < self.target_start + self.target_len
        return self.source_start <= line < self.source_start + self.source_len


@dataclass
class FileDiff:
    path: str
    old_path: str | None
    status: str  # added, removed, modified, renamed
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def additions(self) -> int:
        return sum(len(h.added) for h in self.hunks)

    @property
    def deletions(self) -> int:
        return sum(len(h.removed) for h in self.hunks)


def hunk_ref(path: str, h: Hunk) -> str:
    return f"hunk:{path}:{h.target_start}"


def parse_diff(text: str) -> list[FileDiff]:
    try:
        return _parse_unidiff(text)
    except (UnidiffParseError, ValueError, IndexError):
        return _parse_simple(text)


def _strip(p: str | None) -> str | None:
    if not p or p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def _parse_unidiff(text: str) -> list[FileDiff]:
    out = []
    for pf in PatchSet(text):
        old, new = _strip(pf.source_file), _strip(pf.target_file)
        status = ("added" if pf.is_added_file else "removed" if pf.is_removed_file
                  else "renamed" if old and new and old != new else "modified")
        fd = FileDiff(new or old or pf.path, old, status)
        for h in pf:
            hunk = Hunk(h.target_start, h.source_start, target_len=h.target_length, source_len=h.source_length)
            for ln in h:
                if ln.is_added:
                    hunk.added.append((ln.target_line_no, ln.value.rstrip("\n")))
                elif ln.is_removed:
                    hunk.removed.append((ln.source_line_no, ln.value.rstrip("\n")))
            fd.hunks.append(hunk)
        out.append(fd)
    return out


def _parse_simple(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    nxt = old_n = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur, hunk = FileDiff("", None, "modified"), None
            files.append(cur)
        elif cur is not None and hunk is None and line.startswith("--- "):
            cur.old_path = _strip(line[4:].strip())
        elif cur is not None and hunk is None and line.startswith("+++ "):
            cur.path = _strip(line[4:].strip()) or cur.old_path or ""
        elif cur is not None and line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                continue
            hunk = Hunk(int(m[3]), int(m[1]), target_len=int(m[4] if m[4] is not None else 1),
                        source_len=int(m[2] if m[2] is not None else 1))
            nxt, old_n = int(m[3]), int(m[1])
            cur.hunks.append(hunk)
            cur.path = cur.path or cur.old_path or ""
        elif hunk is not None:
            if line.startswith("+"):
                hunk.added.append((nxt, line[1:]))
                nxt += 1
            elif line.startswith("-"):
                hunk.removed.append((old_n, line[1:]))
                old_n += 1
            elif not line.startswith("\\"):
                nxt += 1
                old_n += 1
    return [f for f in files if f.path]


def added_lines(files: list[FileDiff], min_len: int = 12) -> set[str]:
    return {code.strip() for f in files for h in f.hunks for _, code in h.added if len(code.strip()) >= min_len}
