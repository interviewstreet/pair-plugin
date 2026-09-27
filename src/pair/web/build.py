import shutil
import subprocess
import time
from pathlib import Path

from pair.config import logs_dir

WEB_DIR = Path(__file__).resolve().parents[3] / "web"
SOURCES = ("app", "components", "lib", "public", "package.json", "bun.lock", "next.config.ts", "tsconfig.json")
STEP_TIMEOUT_S = 600


def _say(msg: str) -> None:
    print(msg, flush=True)


def _files(p: Path):
    if p.is_file():
        yield p
    elif p.is_dir():
        yield from (f for f in p.rglob("*") if f.is_file())


def newest_source(web: Path) -> float:
    return max((f.stat().st_mtime for name in SOURCES for f in _files(web / name)), default=0.0)


def needs_build(web: Path = WEB_DIR) -> bool:
    index = web / "out" / "index.html"
    return not index.is_file() or index.stat().st_mtime < newest_source(web)


def ensure_built(web: Path = WEB_DIR, say=_say) -> str:
    """Build web/out if it's missing or older than the sources.

    Returns "fresh", "built", "no-source", "no-bun" or "failed". Without web/out the server serves the placeholder page.
    """
    if not (web / "package.json").is_file():
        return "no-source"
    if not needs_build(web):
        return "fresh"
    bun = shutil.which("bun")
    if bun is None:
        say("pair: bun isn't installed, so the review page can't be built"
            + ("; serving the previous build." if (web / "out").is_dir() else "; serving the basic page instead.")
            + " Install it from https://bun.sh and restart `pair-brief serve`.")
        return "no-bun"
    steps = ([["install", "--frozen-lockfile"]] if not (web / "node_modules").is_dir() else []) + [["run", "build"]]
    log_path = logs_dir() / "web-build.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    say("pair: building the review page…")
    t0 = time.perf_counter()
    with log_path.open("a") as log:
        for args in steps:
            log.write(f"\n$ bun {' '.join(args)}  # {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            log.flush()
            try:
                rc = subprocess.run([bun, *args], cwd=web, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, timeout=STEP_TIMEOUT_S).returncode
            except subprocess.TimeoutExpired:
                rc = "timeout"
            if rc != 0:
                log.write(f"exit {rc}\n")
                say(f"pair: `bun {' '.join(args)}` failed ({rc}); see {log_path}."
                    + (" Serving the previous build." if (web / "out").is_dir() else " Serving the basic page."))
                return "failed"
    say(f"pair: built the review page in {time.perf_counter() - t0:.0f} s")
    return "built"
