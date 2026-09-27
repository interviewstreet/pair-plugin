import functools
import re
import subprocess
from pathlib import Path

from pair.config import HOME, clone_roots

# Worktree layouts whose folders are often deleted after a PR merges.
LAYOUTS = [
    re.compile(r"^/Users/[^/]+/awt/workspaces/(?P<repo>[^/]+)/(?P<branch>[^/]+)"),
    re.compile(r"^/Users/[^/]+/orca/workspaces/(?P<repo>[^/]+)/(?P<branch>[^/]+)"),
]


def normalize_remote(url: str) -> str:
    url = url.strip()
    url = re.sub(r"^(git@github\.com:|ssh://git@github\.com/|https?://github\.com/|https?://[^@/]+@github\.com/)", "",
                 url)
    return re.sub(r"\.git$", "", url).strip("/").lower()


_ORIGIN_URL = re.compile(r'^\[remote "origin"\][^\[]*?^\s*url\s*=\s*(\S+)', re.M | re.S)


def _origin_from_config(root: Path) -> str | None:
    """Read origin's URL from the git config without spawning git. Handles linked worktrees."""
    dot = root / ".git"
    try:
        if dot.is_file():
            m = re.match(r"gitdir:\s*(.+)", dot.read_text().strip())
            if not m:
                return None
            gitdir = Path(m[1]) if Path(m[1]).is_absolute() else root / m[1]
            common = gitdir / "commondir"
            if common.exists():
                gitdir = (gitdir / common.read_text().strip()).resolve()
        else:
            gitdir = dot
        m = _ORIGIN_URL.search((gitdir / "config").read_text())
    except OSError:
        return None
    return m[1] if m else None


@functools.cache
def remote(root: str) -> str:
    if url := _origin_from_config(Path(root)):
        return normalize_remote(url)
    try:
        out = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return normalize_remote(out)


@functools.cache
def _locate_dir(d: str) -> tuple[str, str] | None:
    p = Path(d)
    while p != p.parent:
        if (p / ".git").exists():
            return remote(str(p)), str(p)
        p = p.parent
    return None


@functools.cache
def _repo_for_name(name: str) -> str | None:
    """Full owner/repo for a repo folder name, from any live clone with that name."""
    for c in _clone_candidates(name):
        if (c / ".git").exists() and (r := remote(str(c))):
            return r
    return None


def _clone_candidates(name: str) -> list[Path]:
    dev, *workspaces = clone_roots()
    out = [dev / name]
    for root in workspaces:
        out += sorted((root / name).glob("*"))
    return out


def locate(path: str, repo: str | None = None) -> tuple[str, str] | None:
    """(owner/repo, worktree root) for an absolute path, or None if it isn't in a known repo."""
    if not path.startswith("/"):
        return None
    found = _locate_dir(str(Path(path).parent))
    for rx in LAYOUTS:
        m = rx.match(path)
        if not m or (found and len(found[1]) >= len(m.group(0))):
            continue
        name = m["repo"].lower()
        if repo and repo.split("/")[1] == name:
            return repo, m.group(0)
        full = _repo_for_name(name)
        return (full, m.group(0)) if full else None
    return found if found and found[0] else None


def rel_path(abs_path: str, repo: str) -> str | None:
    """The path relative to its worktree root, if it lies in `repo`."""
    loc = locate(abs_path, repo)
    if not loc or loc[0] != repo:
        return None
    rel = abs_path[len(loc[1]):].lstrip("/")
    return rel or None


def dir_in_repo(d: str | None, repo: str) -> bool:
    if not d:
        return False
    loc = locate(d.rstrip("/") + "/x", repo)
    return bool(loc and loc[0] == repo)


def find_clone(repo: str) -> str | None:
    """A local clone whose origin is `repo`: ~/dev/<name>, then awt and orca workspaces."""
    for c in _clone_candidates(repo.split("/")[1]):
        if (c / ".git").exists() and remote(str(c)) == repo:
            return str(c)
    return None


def clear_caches() -> None:
    remote.cache_clear()
    _locate_dir.cache_clear()
    _repo_for_name.cache_clear()


__all__ = ["HOME", "LAYOUTS", "clear_caches", "dir_in_repo", "find_clone", "locate", "normalize_remote", "rel_path"]
