import fcntl
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pair.config import Config, briefs_dir
from pair.diff import parse_diff
from pair.extract import extract, pushback_hints, top_pushback
from pair.github import fetch_pr
from pair.match import match
from pair.readers import discover, parse_all
from pair.risky import risky_hunks
from pair.util import dumps, parse_pr_spec, review_key, write_atomic

# Sections the subagents write, with their item limits. pair-summary writes both summary and look_first.
LIMITS = {"side_effects": 10, "plan_vs_result": 8, "pushback": 8, "summary": 1, "look_first": 7}
WRITERS = {"side_effects": "pair-side-effects", "plan_vs_result": "pair-plan-check", "pushback": "pair-pushback",
           "summary": "pair-summary"}
MODEL_SECTIONS = list(LIMITS)
STALE_RUN_SECONDS = 15 * 60
TEXT_CHARS = 1500
REF_RX = re.compile(r"(?:claude|codex):[\w.-]+:[\w.-]+|pr:body")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def brief_dir(repo: str, number: int, head_sha: str) -> Path:
    return briefs_dir() / review_key(repo, number) / head_sha


def latest_dir(key: str) -> Path | None:
    root = briefs_dir() / key
    dirs = [d for d in root.glob("*") if (d / "brief.json").exists()] if root.exists() else []
    return max(dirs, key=lambda d: (d / "brief.json").stat().st_mtime) if dirs else None


def review_url(cfg: Config, key: str) -> str:
    return f"{cfg.base_url}/review/?key={key}"


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- inputs for the section writers

def _block(text: str, pad: str, width: int = 400) -> list[str]:
    out = []
    for line in str(text).splitlines() or [""]:
        while len(line) > width:
            out.append(pad + line[:width])
            line = line[width:]
        out.append(pad + line)
    return out


def _scalar(prefix: str, v, pad: str) -> list[str]:
    if isinstance(v, str) and ("\n" in v or len(v) > 160):
        return [prefix + " |"] + _block(v, pad + "  ")
    if v is None:
        return [prefix + " null"]
    if isinstance(v, bool):
        return [prefix + (" true" if v else " false")]
    if isinstance(v, (list, dict)):
        return [prefix + (" []" if isinstance(v, list) else " {}")]
    return [f"{prefix} {v}"]


def render(obj, indent: int = 0) -> list[str]:
    """YAML-like text with long strings wrapped, so the Read tool never truncates a line."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines += render(v, indent + 1)
            else:
                lines += _scalar(f"{pad}{k}:", v, pad)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)) and v:
                sub = render(v, indent + 1)
                sub[0] = pad + "- " + sub[0].lstrip()
                lines += sub
            else:
                lines += _scalar(f"{pad}-", v, pad)
    else:
        lines += _scalar(pad.rstrip() or "", obj, pad)
    return lines


def refs_in(obj) -> list[str]:
    return sorted(set(REF_RX.findall(json.dumps(obj))))


def _fit_plans(plans: list[dict], budget: int = 20000) -> list[dict]:
    if not plans:
        return []
    each = max(1500, budget // len(plans))
    return [{**p, "text": p["text"][:each]} for p in plans]


def section_inputs(ev: dict, sections: dict) -> dict[str, dict]:
    """Inputs for the three parallel writers. A writer with nothing to read is left out."""
    pr = ev["pr"]
    basics = {"repo": pr["repo"], "number": pr["number"], "title": pr["title"], "body": pr["body"][:3000]}
    hunk_refs = sorted(r for r in ev["refs"] if r.startswith("hunk:"))
    file_refs = sorted(r for r in ev["refs"] if r.startswith("file:"))
    out: dict[str, dict] = {}
    if ev["risky_hunks"]:
        out["side_effects"] = {"pr": basics, "risky_hunks": [{k: h[k] for k in ("ref", "tags", "added")}
                                                             for h in ev["risky_hunks"]],
                               "allowed_refs": [h["ref"] for h in ev["risky_hunks"]]}
    plan_in = {"pr": basics, "ask": sections["ask"], "plans": _fit_plans(ev["plans"]),
               "files": [f"{f['path']} (+{f['additions']} -{f['deletions']})" for f in ev["files"]],
               "churn": sections["churn"], "final_messages": ev["final_messages"][-25:]}
    if sections["ask"] or ev["plans"]:
        out["plan_vs_result"] = {**plan_in, "allowed_refs": refs_in(plan_in) + file_refs + hunk_refs}
    cands = top_pushback(ev["pushback_candidates"])
    if cands or sections["decisions"]:
        push_in = {"candidates": [{**{k: c[k] for k in ("ref", "prompt", "interrupted", "interrupted_before",
                                                           "previous_agent_message")},
                                   "hints": pushback_hints(c["prompt"])} for c in cands],
                   "decisions": [{"ref": d["refs"][0], "text": d["text"]} for d in sections["decisions"]]}
        out["pushback"] = {**push_in, "allowed_refs": refs_in(push_in)}
    return out


def summary_input(ev: dict, brief: dict) -> dict:
    sections = {k: v for k, v in brief["sections"].items() if k not in ("summary", "look_first")}
    hunk_refs = sorted(r for r in ev["refs"] if r.startswith("hunk:"))
    pr = ev["pr"]
    return {
        "pr": {"repo": pr["repo"], "number": pr["number"], "title": pr["title"], "body": pr["body"][:3000]},
        "files": [f"{f['path']} (+{f['additions']} -{f['deletions']})" for f in ev["files"]],
        "risky_hunks": [f"{h['ref']} [{', '.join(h['tags'])}]" for h in ev["risky_hunks"]],
        "sections": {k: [{kk: vv for kk, vv in it.items() if kk in ("text", "refs", "status", "superseded")}
                         for it in v] for k, v in sections.items()},
        "still_pending": [s for s in brief["pending"] if s not in ("summary", "look_first")],
        "allowed_refs": sorted(set(refs_in(sections)) | set(hunk_refs)),
    }


def write_input(d: Path, section: str, data: dict) -> Path:
    path = d / "inputs" / f"{section}.md"
    header = [f"# Input for the {section} section", "",
              "Cite refs exactly as they appear under allowed_refs.", ""]
    write_atomic(path, "\n".join(header + render(data)) + "\n")
    return path


def refresh_summary_input(d: Path) -> Path | None:
    ev, brief = load_json(d / "evidence.json"), load_json(d / "brief.json")
    if not ev or not brief:
        return None
    return write_input(d, "summary", summary_input(ev, brief))


# ---------------------------------------------------------------- prepare

def _session_view(s: dict) -> dict:
    return {k: s[k] for k in ("agent", "id", "role", "opened_pr", "title", "started_at")}


def prepare(spec: str, cfg: Config, force: bool = False) -> dict:
    t0 = time.perf_counter()
    marks: dict[str, float] = {}

    def mark(name: str, since: float) -> float:
        marks[name] = round(time.perf_counter() - since, 3)
        return time.perf_counter()

    repo, n = parse_pr_spec(spec)
    key = review_key(repo, n)
    pr = fetch_pr(repo, n, cfg)
    t = mark("gh", t0)
    marks.update({f"gh_{k}": v for k, v in pr.timings.items()})
    d = brief_dir(pr.repo, pr.number, pr.head_oid)
    base = {"key": key, "dir": str(d), "url": review_url(cfg, key), "head_sha": pr.head_oid}

    existing = load_json(d / "brief.json")
    if existing and not force:
        age = time.time() - (d / "brief.json").stat().st_mtime
        if not existing.get("pending"):
            return {**base, "status": "ready", "write": [], "pending": []}
        if age < STALE_RUN_SECONDS:
            return {**base, "status": "in_progress", "write": [], "pending": existing["pending"]}

    paths = discover(pr.window_start)
    sessions = parse_all(paths)
    t = mark("parse", t)
    files = parse_diff(pr.diff)
    matches = match(pr, sessions, files)
    t = mark("match", t)
    ex = extract(pr, matches, files)
    risky = risky_hunks(files)
    t = mark("extract", t)

    kept = [m for m in matches if m.kept]
    diff_files = {f.path: f for f in files}
    file_list = [{"path": f["path"], "additions": f["additions"], "deletions": f["deletions"]} for f in pr.files]
    file_list += [{"path": p, "additions": f.additions, "deletions": f.deletions}
                  for p, f in diff_files.items() if p not in {x["path"] for x in file_list}]
    evidence = {
        "pr": pr.basics(), "window": [pr.window_start.isoformat(), pr.window_end.isoformat()],
        "files": file_list, "sessions": [m.summary() for m in kept],
        "related": [m.summary() for m in matches if not m.kept],
        "refs": sorted(ex["refs"]), "risky_hunks": risky, "plans": ex["plans"],
        "final_messages": ex["final_messages"], "pushback_candidates": ex["pushback_candidates"],
        "sections": ex["sections"], "files_parsed": len(paths),
    }
    inputs = section_inputs(evidence, ex["sections"])
    sections = dict(ex["sections"])
    pending = []
    for s in ("side_effects", "plan_vs_result", "pushback"):
        if s in inputs:
            pending.append(s)
        else:
            sections[s] = []
    pending += ["summary", "look_first"]
    brief = {"pr": {k: v for k, v in pr.basics().items() if k in ("repo", "number", "title", "url", "head_branch",
                                                                   "base_branch", "body")},
             "head_sha": pr.head_oid, "sessions": [_session_view(s) for s in evidence["sessions"]],
             "sections": sections, "pending": pending, "created_at": now_iso(), "updated_at": now_iso()}

    d.mkdir(parents=True, exist_ok=True)
    write_atomic(d / "diff.patch", pr.diff)
    write_atomic(d / "risky_hunks.json", dumps(risky))
    write_atomic(d / "evidence.json", dumps(evidence))
    input_paths = {s: str(write_input(d, s, data)) for s, data in inputs.items()}
    with locked(d):
        write_atomic(d / "brief.json", dumps(brief))
    input_paths["summary"] = str(refresh_summary_input(d))
    (d / "out").mkdir(exist_ok=True)
    mark("write", t)
    marks["total"] = round(time.perf_counter() - t0, 3)
    return {**base, "status": "started", "write": [s for s in pending if s != "look_first"],
            "agents": {s: WRITERS[s] for s in pending if s in WRITERS}, "inputs": input_paths,
            "outputs": {s: str(output_path(d, s)) for s in pending}, "pending": pending,
            "sessions": len(kept), "discussion_only": sum(1 for m in kept if m.role == "discussion"),
            "related": len(matches) - len(kept), "timings_s": marks}


# ---------------------------------------------------------------- save

@contextmanager
def locked(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".brief.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class SaveError(Exception):
    pass


def _parse_items(raw: str):
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise SaveError(f"input is not valid JSON: {e}") from e


def _clean_extra(v):
    if isinstance(v, str):
        return v[:TEXT_CHARS]
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return [x[:300] for x in v[:10]]
    return None


def validate(section: str, items, known: set[str]) -> tuple[list[dict], dict]:
    if not isinstance(items, list):
        raise SaveError(f"{section}: expected a JSON list of items")
    kept, dropped_refs, dropped_items = [], 0, 0
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("text"), str) or not it["text"].strip():
            dropped_items += 1
            continue
        refs = it.get("refs") if isinstance(it.get("refs"), list) else []
        good = list(dict.fromkeys(r for r in refs if isinstance(r, str) and r in known))
        dropped_refs += len(refs) - len(good)
        if section == "look_first" and not any(r.startswith("hunk:") for r in good):
            good = []
        if section == "summary" and not good:
            good = ["pr:body"]
        if not good:
            dropped_items += 1
            continue
        item = {"text": it["text"].strip()[:TEXT_CHARS], "refs": good}
        for k, v in it.items():
            if k not in ("text", "refs") and (cv := _clean_extra(v)) is not None:
                item[k] = cv
        kept.append(item)
    over = max(0, len(kept) - LIMITS[section])
    report = {"section": section, "kept": min(len(kept), LIMITS[section]), "dropped_items": dropped_items + over,
              "dropped_refs": dropped_refs}
    if items and not kept:
        hint = " look_first items each need a hunk: ref." if section == "look_first" else ""
        raise SaveError(f"{section}: none of the {len(items)} items cite a ref from allowed_refs.{hint}")
    return kept[:LIMITS[section]], report


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def _apply_supersedes(brief: dict, items: list[dict]) -> int:
    """Mark the decisions that pushback items reverse. Keeps only `supersedes` refs that name a decision."""
    by_ref: dict[str, list[dict]] = {}
    for d in brief["sections"].get("decisions", []):
        if d.get("refs"):
            by_ref.setdefault(d["refs"][0], []).append(d)
    n = 0
    for it in items:
        targets = it.pop("supersedes", None)
        targets = [targets] if isinstance(targets, str) else targets or []
        kept = []
        for ref in targets:
            # Questions asked in one AskUserQuestion call share a ref; pick the one the item talks about.
            open_ = [d for d in by_ref.get(ref, []) if not d.get("superseded")]
            if not open_:
                continue
            words = _words(it["text"])
            d = max(open_, key=lambda x: len(words & _words(x["text"])))
            d["superseded"], d["superseded_by"] = True, it["refs"][0]
            kept.append(ref)
            n += 1
        if kept:
            it["supersedes"] = kept
    return n


def save(d: Path, section: str, raw: str) -> dict:
    if section not in LIMITS:
        raise SaveError(f"unknown section {section!r}; expected one of {', '.join(LIMITS)}")
    ev = load_json(d / "evidence.json")
    if not ev or not (d / "brief.json").exists():
        raise SaveError(f"no brief in {d}; run pair-brief prepare first")
    items, report = validate(section, _parse_items(raw), set(ev["refs"]))
    with locked(d):
        brief = load_json(d / "brief.json")
        brief["sections"][section] = items
        if section == "pushback":
            report["superseded_decisions"] = _apply_supersedes(brief, items)
        brief["pending"] = [s for s in brief["pending"] if s != section]
        brief["updated_at"] = now_iso()
        write_atomic(d / "brief.json", dumps(brief))
    refresh_summary_input(d)
    report["pending"] = brief["pending"]
    return report


def output_path(d: Path, section: str) -> Path:
    """Where a writer subagent writes its JSON list. The plugin's Write hook saves it from there."""
    return d / "out" / f"{section}.json"


def save_output(d: Path, section: str) -> dict:
    """Save a writer's output file, then move it aside so a retry writes a fresh file."""
    path = output_path(d, section)
    if not path.exists():
        raise SaveError(f"write the JSON list to {path} first")
    raw = path.read_text()
    n = len(list(path.parent.glob(f"{section}.*.json"))) + 1
    path.rename(path.with_name(f"{section}.{n}.json"))
    return save(d, section, raw)
