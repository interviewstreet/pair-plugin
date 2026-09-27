import os

import pytest

from pair.web import build

FAKE_BUN = """#!/bin/sh
echo "$@" >> "$FAKE_BUN_CALLS"
case "$1" in
  install) mkdir -p node_modules ;;
  run)
    if [ -n "$FAKE_BUN_FAIL" ]; then echo "type error in page.tsx" >&2; exit 3; fi
    mkdir -p out && echo ok > out/index.html ;;
esac
"""


@pytest.fixture
def web(tmp_path, monkeypatch):
    w = tmp_path / "web"
    (w / "app").mkdir(parents=True)
    (w / "app" / "page.tsx").write_text("export default 1")
    (w / "package.json").write_text("{}")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "bun").write_text(FAKE_BUN)
    (bindir / "bun").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("FAKE_BUN_CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("PAIR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FAKE_BUN_FAIL", raising=False)
    return w


def calls(web) -> list[str]:
    path = web.parent / "calls"
    return path.read_text().splitlines() if path.exists() else []


def touch_after_build(path, web):
    out = (web / "out" / "index.html").stat().st_mtime
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed")
    os.utime(path, (out + 10, out + 10))


def test_installs_then_builds_when_nothing_exists(web):
    said = []
    assert build.ensure_built(web, say=said.append) == "built"
    assert calls(web) == ["install --frozen-lockfile", "run build"]
    assert (web / "out" / "index.html").is_file()
    log = (web.parent / "home" / "logs" / "web-build.log").read_text()
    assert "$ bun install --frozen-lockfile" in log and "$ bun run build" in log
    assert said[-1].startswith("pair: built the review page")


def test_skips_a_fresh_build(web):
    build.ensure_built(web, say=lambda m: None)
    assert build.ensure_built(web, say=lambda m: None) == "fresh"
    assert calls(web) == ["install --frozen-lockfile", "run build"]


def test_rebuilds_without_reinstalling_when_a_source_is_newer(web):
    build.ensure_built(web, say=lambda m: None)
    touch_after_build(web / "components" / "Thing.tsx", web)
    assert build.ensure_built(web, say=lambda m: None) == "built"
    assert calls(web)[-1] == "run build"
    assert calls(web).count("install --frozen-lockfile") == 1


def test_ignores_files_outside_the_sources(web):
    build.ensure_built(web, say=lambda m: None)
    touch_after_build(web / "tests" / "x.test.ts", web)
    touch_after_build(web / "node_modules" / "pkg" / "index.js", web)
    assert build.ensure_built(web, say=lambda m: None) == "fresh"


def test_says_so_when_bun_is_missing(web, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    if build.shutil.which("bun"):
        pytest.skip("bun is installed system-wide")
    said = []
    assert build.ensure_built(web, say=said.append) == "no-bun"
    assert "bun isn't installed" in said[0] and "basic page" in said[0]
    assert not (web / "out").exists()


def test_reports_a_failed_build(web, monkeypatch):
    monkeypatch.setenv("FAKE_BUN_FAIL", "1")
    said = []
    assert build.ensure_built(web, say=said.append) == "failed"
    assert "`bun run build` failed" in said[-1] and "web-build.log" in said[-1]
    assert "type error in page.tsx" in (web.parent / "home" / "logs" / "web-build.log").read_text()
    assert not (web / "out").exists()


def test_does_nothing_without_a_web_app(tmp_path):
    assert build.ensure_built(tmp_path / "missing", say=lambda m: None) == "no-source"


def test_serve_builds_before_creating_the_app(tmp_path, monkeypatch):
    import uvicorn

    from pair import cli
    from pair.web import app as web_app

    monkeypatch.setenv("PAIR_CONFIG", str(tmp_path / "no-config.toml"))
    order = []
    monkeypatch.setattr(build, "ensure_built", lambda *a, **k: order.append("build") or "fresh")
    monkeypatch.setattr(web_app, "create_app", lambda cfg: order.append("app") or object())
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: order.append(("run", kw["port"])))
    assert cli.cmd_serve(None) == 0
    assert order == ["build", "app", ("run", 7788)]
