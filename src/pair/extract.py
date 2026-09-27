import re

from pair.diff import FileDiff, hunk_ref
from pair.github import PR
from pair.match import Match
from pair.model import Turn
from pair.repos import rel_path
from pair.util import is_doc

TEST_START = re.compile(
    r"^(?:\w+=\S*\s+)*(?:timeout \d+\s+)?(go test|make (test|check)[\w-]*|(python3? -m |uv run )?pytest|"
    r"npm (run )?test|(bundle exec )?rspec|mvn test|cargo test|bun (run )?test|yarn test|(npx |bunx )?(vitest|jest))\b")
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", re.S)
UNCHECKED = re.compile(r"^\s*[-*] \[ \] (.+)$", re.M)
LOOSE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")

TESTING_WORDS = re.compile(r"\b(test(ed|ing)?|working|works|verify|verified|does it work|did you run)\b", re.I)
REVERSAL_WORDS = re.compile(r"\b(instead|rather|revert|undo|go back|not what|wrong|don't|dont|shouldn't|remove|"
                            r"simplify|too (complex|much|slow))\b", re.I)
LAYOUT_WORDS = re.compile(r"\b(left|right|top|bottom|default)\b", re.I)
PUSHBACK_HINTS = [
    (TESTING_WORDS, 3),
    (REVERSAL_WORDS, 2),
    (re.compile(r"\b(left|right|default|should)\b", re.I), 1),
    (re.compile(r"\?"), 1),
]
HINT_NAMES = [("asks about testing", TESTING_WORDS), ("reversal words", REVERSAL_WORDS),
              ("layout or default change", LAYOUT_WORDS)]
PUSHBACK_TOP = 60
STATUS_ORDER = {"failed": 0, "never_run": 1, "stale": 2, "passed": 3}


REDIRECT = re.compile(r"\s*(\d?>&\d|\d?>>?\s*\S+)")
VERIFICATION_MAX = 20


def find_test_steps(cmd: str) -> list[str]:
    """The test steps inside a shell command, normalized so reruns of the same test group together."""
    cleaned = HEREDOC.sub("", cmd)
    segs = []
    for seg in re.split(r"\n|&&|\|\||;|\|", cleaned):
        seg = seg.strip()
        if TEST_START.match(seg):
            segs.append(" ".join(REDIRECT.sub("", seg).split()))
    return segs


def is_test_command(cmd: str) -> bool:
    return bool(find_test_steps(cmd))


def pushback_score(c: dict) -> int:
    score = 3 if (c["interrupted"] or c["interrupted_before"]) else 0
    score += sum(w for rx, w in PUSHBACK_HINTS if rx.search(c["prompt"]))
    return score + (1 if len(c["prompt"]) < 200 else 0)


def pushback_hints(prompt: str) -> list[str]:
    return [name for name, rx in HINT_NAMES if rx.search(prompt)]


def _main_turns(m: Match) -> list[Turn]:
    return [t for t in m.all_turns if not t.subagent]


def ask(builds: list[Match]) -> list[dict]:
    items = []
    for m in builds:
        turns = _main_turns(m) or m.build_turns
        first = min(turns, key=lambda t: t.started)
        items.append({"text": first.prompt.strip()[:4000], "refs": [first.ref], "session": m.key,
                      "at": first.started.isoformat()})
    return sorted(items, key=lambda it: it["at"])


def verification(pr: PR, builds: list[Match], kept: list[Match]) -> list[dict]:
    code_files = {f["path"] for f in pr.files if not is_doc(f["path"])}
    edit_times = [e.ts for m in builds for t in m.build_turns for e in t.edits
                  if rel_path(e.path, pr.repo) in code_files]
    last_edit = max(edit_times) if edit_times else None
    runs: dict[str, list[tuple]] = {}
    for m in kept:
        for t in m.all_turns:
            for c in t.commands:
                if segs := find_test_steps(c.cmd):
                    runs.setdefault(" ; ".join(segs)[:300], []).append((c, t))
    items = []
    for key, rs in runs.items():
        rs.sort(key=lambda r: r[0].ts)
        last, _ = rs[-1]
        status = "stale" if last_edit and last.ts < last_edit else "failed" if last.failed else "passed"
        items.append({"text": key, "status": status, "runs": len(rs), "last_run": last.ts.isoformat(),
                      "command": " ".join(last.cmd.split())[:300],
                      "refs": list(dict.fromkeys(t.ref for _, t in reversed(rs)))[:3]})
    items.sort(key=lambda it: it["last_run"], reverse=True)
    never = [{"text": item.strip()[:300], "status": "never_run", "runs": 0, "refs": ["pr:body"]}
             for item in UNCHECKED.findall(pr.body)]
    ordered = sorted(items + never, key=lambda it: STATUS_ORDER[it["status"]])
    return ordered[:VERIFICATION_MAX]


def decisions(kept: list[Match]) -> list[dict]:
    qs = sorted({q.ref + q.question: q for m in kept for t in m.all_turns for q in t.questions if q.answer}.values(),
                key=lambda q: q.ts)
    items = []
    for i, q in enumerate(qs):
        item = {"text": f"Q: {q.question} A: {q.answer}", "question": q.question, "answer": q.answer,
                "options": q.options, "refs": [q.ref], "superseded": False}
        norm = " ".join(q.question.lower().split())
        later = next((r for r in qs[i + 1:] if " ".join(r.question.lower().split()) == norm), None)
        if later and later.answer != q.answer:
            item["superseded"], item["superseded_by"] = True, later.ref
        items.append(item)
    return items


def churn(pr: PR, builds: list[Match]) -> list[dict]:
    pr_files = {f["path"] for f in pr.files}
    counts: dict[str, dict] = {}
    for m in builds:
        for t in m.build_turns:
            for e in t.edits:
                rel = rel_path(e.path, pr.repo)
                if rel in pr_files:
                    c = counts.setdefault(rel, {"edits": 0, "turns": {}, "sessions": set()})
                    c["edits"] += 1
                    c["turns"][t.ref] = t.started
                    c["sessions"].add(m.key)
    items = []
    for path, c in sorted(counts.items(), key=lambda kv: -kv[1]["edits"]):
        if c["edits"] < 3:
            continue
        turns = sorted(c["turns"], key=c["turns"].get)
        items.append({"text": f"{path}: {c['edits']} edits across {len(turns)} turns in {len(c['sessions'])} "
                              f"session{'s' if len(c['sessions']) != 1 else ''}",
                      "path": path, "edits": c["edits"], "turns": len(turns), "refs": turns[:3]})
    return items[:8]


def loose_ends(files: list[FileDiff]) -> list[dict]:
    items = [{"text": f"{f.path}:{ln}: {code.strip()[:200]}", "refs": [hunk_ref(f.path, h)]}
             for f in files for h in f.hunks for ln, code in h.added if LOOSE.search(code)]
    return items[:8]


def pushback_candidates(builds: list[Match]) -> list[dict]:
    out = []
    for m in builds:
        main = [t for t in m.context_turns if not t.subagent] or _main_turns(m)
        for prev, t in zip([None] + main[:-1], main):
            if prev is None and not t.interrupted:
                continue
            out.append({
                "ref": t.ref, "at": t.started.isoformat(), "prompt": t.prompt.strip()[:600],
                "interrupted": t.interrupted, "interrupted_before": bool(prev and prev.interrupted),
                "previous_agent_message": prev.assistant_text[-1][-500:] if prev and prev.assistant_text else "",
            })
    for c in out:
        c["score"] = pushback_score(c)
    return out


def top_pushback(cands: list[dict], n: int = PUSHBACK_TOP) -> list[dict]:
    keep = {c["ref"] for c in sorted(cands, key=lambda c: -c["score"])[:n]}
    return [c for c in sorted(cands, key=lambda c: c["at"]) if c["ref"] in keep]


def plans(kept: list[Match]) -> list[dict]:
    seen, out = set(), []
    for m in kept:
        for t in m.all_turns:
            for p in t.plans:
                if p.text in seen:
                    continue
                seen.add(p.text)
                out.append({"ref": p.ref, "path": p.path, "at": t.started.isoformat(), "text": p.text[:6000]})
    return sorted(out, key=lambda p: p["at"])


def final_messages(kept: list[Match]) -> list[dict]:
    out = [{"ref": t.ref, "at": t.started.isoformat(), "text": t.assistant_text[-1][-800:]}
           for m in kept if m.role == "build" for t in _main_turns(m) if t.assistant_text]
    return sorted(out, key=lambda x: x["at"])


def evidence_refs(kept: list[Match], files: list[FileDiff]) -> set[str]:
    refs = {"pr:body"}
    for m in kept:
        for t in m.all_turns:
            refs.add(t.ref)
            refs.update(q.ref for q in t.questions)
            refs.update(p.ref for p in t.plans)
    for f in files:
        refs.add(f"file:{f.path}")
        refs.update(hunk_ref(f.path, h) for h in f.hunks)
    return refs


def extract(pr: PR, matches: list[Match], files: list[FileDiff]) -> dict:
    kept = [m for m in matches if m.kept]
    builds = [m for m in kept if m.role == "build"]
    return {
        "sections": {
            "ask": ask(builds),
            "verification": verification(pr, builds, kept),
            "decisions": decisions(kept),
            "churn": churn(pr, builds),
            "loose_ends": loose_ends(files),
        },
        "pushback_candidates": pushback_candidates(builds),
        "plans": plans(kept),
        "final_messages": final_messages(kept),
        "refs": evidence_refs(kept, files),
    }
