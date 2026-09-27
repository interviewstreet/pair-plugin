import argparse
import json
import sys
import time


def _load_matches(spec: str):
    from pair.config import load_config
    from pair.diff import parse_diff
    from pair.github import fetch_pr
    from pair.match import match
    from pair.readers import discover, parse_all
    from pair.util import parse_pr_spec

    cfg = load_config()
    repo, n = parse_pr_spec(spec)
    marks: dict[str, float] = {}
    t0 = time.perf_counter()
    pr = fetch_pr(repo, n, cfg)
    marks["gh"] = round(time.perf_counter() - t0, 3)
    t = time.perf_counter()
    paths = discover(pr.window_start)
    sessions = parse_all(paths)
    marks["parse"] = round(time.perf_counter() - t, 3)
    t = time.perf_counter()
    diff_files = parse_diff(pr.diff)
    matches = match(pr, sessions, diff_files)
    marks["match"] = round(time.perf_counter() - t, 3)
    return cfg, pr, diff_files, matches, {"files_parsed": len(paths), "sessions": len(sessions), **marks}


def cmd_match(args) -> int:
    _, pr, _, matches, stats = _load_matches(args.pr)
    print(json.dumps({
        "pr": f"{pr.repo}#{pr.number}", "window": [pr.window_start.isoformat(), pr.window_end.isoformat()],
        "kept": [m.summary() for m in matches if m.kept],
        "related": [m.summary() for m in matches if not m.kept],
        "stats": stats,
    }, indent=1))
    return 0


def cmd_prepare(args) -> int:
    from pair.brief import prepare
    from pair.config import load_config
    from pair.github import GhError

    cfg = load_config()
    try:
        out = prepare(args.pr, cfg, force=args.force)
    except (GhError, ValueError) as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        return 1
    if not args.no_serve:
        from pair.server import ensure_server
        out["server"] = ensure_server(cfg)
    print(json.dumps(out, indent=1))
    return 0


def cmd_save(args) -> int:
    from pathlib import Path

    from pair.brief import SaveError, output_path, save, save_output

    d = Path(args.dir)
    try:
        if args.empty:
            report = save(d, args.section, "[]")
        elif args.file:
            report = save(d, args.section, Path(args.file).read_text())
        elif output_path(d, args.section).exists() or sys.stdin.isatty():
            report = save_output(d, args.section)
        else:
            report = save(d, args.section, sys.stdin.read())
    except SaveError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps({"ok": True, **report}))
    return 0


def cmd_submit(args) -> int:
    """Pack the brief for `pr` into a tar.gz and POST it to the review service."""
    from pair.config import load_config
    from pair.github import GhError
    from pair.submit import SubmitError, submit

    cfg = load_config()
    try:
        result = submit(args.pr, cfg, force=args.force)
    except (GhError, SubmitError, ValueError) as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        return 1
    print(json.dumps(result, indent=1))
    return 0


def cmd_status(args) -> int:
    """Fetch the manifest for a submit key (or resolve one from `owner/repo#N`)."""
    from pair.config import load_config
    from pair.submit import SubmitError, status

    cfg = load_config()
    try:
        result = status(args.arg, cfg)
    except (SubmitError, ValueError) as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        return 1
    print(json.dumps(result, indent=1))
    return 0


def cmd_input(args) -> int:
    from pathlib import Path

    from pair.brief import refresh_summary_input

    if args.section != "summary":
        path = Path(args.dir) / "inputs" / f"{args.section}.md"
        print(path if path.exists() else f"no input for {args.section}")
        return 0 if path.exists() else 1
    path = refresh_summary_input(Path(args.dir))
    print(path or "no brief in that folder")
    return 0 if path else 1


def cmd_serve(args) -> int:
    import uvicorn

    from pair.config import load_config
    from pair.web.app import create_app
    from pair.web.build import ensure_built

    cfg = load_config()
    ensure_built()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info", ws_ping_interval=30)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pair-brief", description="Build and serve PR review briefs.")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="Match sessions to a PR and write the data-derived brief")
    p.add_argument("pr", help="owner/repo#N or a PR URL")
    p.add_argument("--force", action="store_true", help="Rebuild even if a brief for this head SHA exists")
    p.add_argument("--no-serve", action="store_true", help="Don't start the review server")
    p.set_defaults(func=cmd_prepare)

    s = sub.add_parser("save", help="Validate a section's JSON list and merge it into brief.json. Reads "
                                    "<dir>/out/<section>.json, or stdin when that file doesn't exist")
    s.add_argument("dir")
    s.add_argument("section")
    s.add_argument("--file", help="Read the JSON list from this file instead")
    s.add_argument("--empty", action="store_true", help="Save an empty section so the page stops waiting for it")
    s.set_defaults(func=cmd_save)

    up = sub.add_parser("submit", help="Prepare the brief and POST the payload to the review service")
    up.add_argument("pr", help="owner/repo#N or a PR URL")
    up.add_argument("--force", action="store_true", help="Ask the service to rebuild even if this key is done")
    up.set_defaults(func=cmd_submit)

    st = sub.add_parser("status", help="Get the manifest of a submitted review (key or owner/repo#N)")
    st.add_argument("arg", help="Submit key <owner>__<repo>__<n>__<sha12> or owner/repo#N")
    st.set_defaults(func=cmd_status)

    i = sub.add_parser("input", help="Print the path of a section writer's input (rebuilds the summary input)")
    i.add_argument("dir")
    i.add_argument("section")
    i.set_defaults(func=cmd_input)

    v = sub.add_parser("serve", help="Run the review API on localhost")
    v.set_defaults(func=cmd_serve)

    m = sub.add_parser("match", help="Debug: print the sessions matched to a PR")
    m.add_argument("pr")
    m.set_defaults(func=cmd_match)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
