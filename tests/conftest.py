import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _copy_templates(src: Path, dst: Path, repo: Path) -> None:
    for f in src.rglob("*"):
        if f.is_file():
            out = dst / f.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f.read_text().replace("__REPO__", str(repo)))


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A git repo whose origin is acme/demo, plus session stores that point into it."""
    root = tmp_path.resolve()
    repo = root / "dev" / "demo"
    (repo / "app").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:acme/demo.git"], check=True)
    _copy_templates(FIXTURES / "claude", root / "claude", repo)
    _copy_templates(FIXTURES / "codex", root / "codex", repo)
    monkeypatch.setenv("PAIR_CLAUDE_DIR", str(root / "claude"))
    monkeypatch.setenv("PAIR_CODEX_DIR", str(root / "codex"))
    monkeypatch.setenv("PAIR_HOME", str(root / "pair-home"))
    monkeypatch.setenv("PAIR_CONFIG", str(root / "no-config.toml"))
    monkeypatch.setenv("PAIR_CLONE_ROOTS", str(root / "dev"))
    from pair import repos
    repos.clear_caches()
    return {"root": root, "repo": repo, "claude": root / "claude", "codex": root / "codex"}


PR7_BODY = "Adds a login flow.\n\n## Test plan\n- [x] pytest\n- [ ] Try the login page in a browser\n"


@pytest.fixture
def pr7():
    from pair.diff import parse_diff
    from pair.github import PR
    from pair.util import ts

    diff = (FIXTURES / "pr7.diff").read_text()
    files = [{"path": f.path, "additions": f.additions, "deletions": f.deletions} for f in parse_diff(diff)]
    # Full 40-char hex SHAs so submit_key(head_sha[:12]) has a valid prefix.
    head = "aaaaaaaabbbbbbbbccccccccdddddddd11112222"
    base = "0000111122223333444455556666777788889999"
    mbase = "9999888877776666555544443333222211110000"
    return PR("acme/demo", 7, "https://github.com/acme/demo/pull/7", "Add login", PR7_BODY, "OPEN", "me",
              "feature/login-flow", "main", head, base, files,
              [{"oid": "c1", "authoredDate": "2026-09-20T09:00:00Z", "committedDate": "2026-09-20T10:06:00Z"}],
              mbase, ts("2026-09-19T00:00:00Z"), diff)


@pytest.fixture
def fixture_text():
    def read(name: str) -> str:
        return (FIXTURES / name).read_text()
    return read


def copy_fixture(name: str, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / name, dst)
    return dst
