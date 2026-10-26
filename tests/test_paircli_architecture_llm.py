import copy
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import paircli_architecture_llm as llm
from paircli_architecture import ArchitectureError
from paircli_architecture_v2 import finalize_v2, validate_v2
from snapshot_factory import HEAD, make_file, make_snapshot, many_files, source_lines

REVIEWS, SESSION = "src/api/reviews.ts", "src/api/session.ts"
STORE, LEGACY, NAMES = "src/db/store.ts", "src/db/legacy.sql", "src/lib/names.ts"
LOCK = "package-lock.json"


# --- a stub draft helper: this file does not depend on the real deterministic draft ----------

def slug(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return text if text[:1].isalpha() else f"n{text}"


def rid(path: str) -> str:
    return slug(path)


def cite(snapshot, path, offset=0, side=None, length=1):
    """A citation of the first hunk of a file (new side unless the file has no new side)."""
    file = next(entry for entry in snapshot["pr"]["files"] if entry["path"] == path)
    if not file["hunks"]:  # a file without line data cannot really be cited; the digest test needs a placeholder
        return {"type": "diff", "path": path, "start": 1, "end": 1, "side": "new"}
    hunk = file["hunks"][0]
    side = side or ("new" if hunk["new_lines"] > 0 else "old")
    start = hunk[f"{side}_start"] + offset
    return {"type": "diff", "path": path, "start": start, "end": start + length - 1, "side": side}


def stub_draft(snapshot, edges=(), depth=2, per_group=5, name=lambda key: slug(key)):
    """A v2 draft shaped like the deterministic one: file rows per group, a routine row, import edges."""
    files = sorted(snapshot["pr"]["files"], key=lambda entry: entry["path"])
    groups: dict[str, str] = {}
    nodes, counts, routine = [], {}, {}
    for entry in files:
        path = entry["path"]
        key = "/".join(path.split("/")[:-1][:depth])
        group = name(key) if key else "root"
        groups.setdefault(group, key)
        if path.endswith("lock.json"):
            routine.setdefault(group, []).append(path)
            continue
        counts[group] = counts.get(group, 0) + 1
        if counts[group] > per_group:
            continue
        nodes.append({"id": rid(path), "label": path.rsplit("/", 1)[-1], "group": group, "kind": "file",
                      "description": f"Changes {path}.", "files": [path], "evidence": [cite(snapshot, path)]})
    for group, paths in sorted(routine.items()):
        nodes.append({"id": f"{group}-routine", "label": f"{len(paths)} routine file", "group": group,
                      "kind": "routine", "description": f"{len(paths)} routine file.", "files": paths, "evidence": []})
    return {
        "title": "Synthetic change", "description": "A deterministic draft for tests.",
        "groups": [{"id": group, "label": (key.rsplit("/", 1)[-1] or "Root").title(), "path": key}
                   for group, key in sorted(groups.items())],
        "nodes": nodes,
        "edges": [{"id": edge_id, "source": rid(source), "target": rid(target), "label": "imports",
                   "claim": f"{source} imports {target}.", "evidence": [cite(snapshot, source, offset)]}
                  for edge_id, source, target, offset in edges],
        "steps": [], "gaps": [],
    }


def world_snapshot():
    return make_snapshot([
        make_file(REVIEWS, "modified", added=source_lines(3), removed=["old reviews line"], start=10),
        make_file(SESSION, "added", added=source_lines(5), start=1),
        make_file(STORE, "modified", added=source_lines(2), removed=["old store line"], start=20),
        make_file(LEGACY, "deleted", removed=source_lines(4), start=1),
        make_file(NAMES, "modified", added=source_lines(2), removed=["old names line"], start=5),
        make_file(LOCK, "modified", added=["a"], removed=["b"], start=1),
    ])


def world():
    snapshot = world_snapshot()
    draft = stub_draft(snapshot, name=lambda key: key.rsplit("/", 1)[-1], edges=[
        ("imp-reviews-store", REVIEWS, STORE, 1),
        ("imp-reviews-session", REVIEWS, SESSION, 0),
        ("imp-names-store", NAMES, STORE, 0),
    ])
    return snapshot, draft


ROW = {path: rid(path) for path in (REVIEWS, SESSION, STORE, LEGACY, NAMES)}
GROUP_ROWS = {"api": [ROW[REVIEWS], ROW[SESSION]], "db": [ROW[STORE], ROW[LEGACY]], "lib": [ROW[NAMES]]}
SCORES = {"groups": [{"id": "api", "score": 9.0}, {"id": "db", "score": 5.0}, {"id": "lib", "score": 1.0}]}


def simple_patch(group, label=None):
    """A valid patch that rewrites the label of the group's first row."""
    return json.dumps({"rows": [{"id": GROUP_ROWS[group][0], "label": label or f"{group} text",
                                 "description": f"New description for {group}."}], "summary": f"{group} summary"})


def api_patch(snapshot, **extra):
    patch = {
        "rows": [{"id": ROW[REVIEWS], "label": "Reviews API", "description": "Validates a review before saving it.",
                  "kind": "service"}],
        "edges": [{"id": "imp-reviews-store", "label": "saves", "claim": "The handler now calls the store."}],
        "summary": "The API validates reviews.",
    }
    patch.update(extra)
    return patch


def merge_answer(snapshot, **extra):
    answer = {
        "title": "Reviews are validated", "description": "Reviews are validated before they are stored.",
        "edges": [{"id": "x-names-reviews", "source": ROW[NAMES], "target": ROW[REVIEWS], "label": "names",
                   "claim": "The handler now uses the name helper.", "evidence": [cite(snapshot, NAMES, 1)]}],
        "steps": [
            {"id": "s1", "when": "after", "edge": "imp-reviews-store", "nodes": [ROW[REVIEWS], ROW[STORE]],
             "text": "The handler now saves reviews through the store.", "evidence": [cite(snapshot, REVIEWS, 1)]},
            {"id": "s2", "when": "before", "nodes": [ROW[LEGACY]], "text": "A legacy SQL file used to exist.",
             "evidence": [cite(snapshot, LEGACY, 0)]},
            {"id": "s3", "when": "after", "edge": "x-names-reviews", "nodes": [ROW[NAMES], ROW[REVIEWS]],
             "text": "Names are now looked up first.", "evidence": [cite(snapshot, NAMES, 1)]},
        ],
        "gaps": ["No tests were changed."],
    }
    answer.update(extra)
    return answer


# --- stub model --------------------------------------------------------------------------

class Stub:
    """A thread-safe stand-in for the model. `answers` maps a group id (or "merge") to text, a list of
    texts (one per attempt), an exception to raise, or a callable(prompt, attempt)."""

    def __init__(self, answers=None, delay=0.0, default=None):
        self.answers = dict(answers or {})
        self.delay = delay
        self.default = default
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.attempts: dict[str, int] = {}

    def __call__(self, prompt, *, timeout=None, model=None):
        match = re.search(r'Group: id "([^"]+)"', prompt)
        who = match.group(1) if match else "merge"
        with self.lock:
            attempt = self.attempts[who] = self.attempts.get(who, 0) + 1
            self.calls.append({"who": who, "prompt": prompt, "timeout": timeout, "model": model, "attempt": attempt})
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            answer = self.answers.get(who, self.default)
            if isinstance(answer, list):
                answer = answer[min(attempt, len(answer)) - 1]
            if callable(answer) and not isinstance(answer, BaseException):
                answer = answer(prompt, attempt)
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, (dict, list)):
                answer = json.dumps(answer)
            return answer
        finally:
            with self.lock:
                self.in_flight -= 1

    def who(self, name):
        return [call for call in self.calls if call["who"] == name]

    @property
    def group_calls(self):
        return [call for call in self.calls if call["who"] != "merge"]


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


def good_answers(snapshot, merge=True):
    answers = {group: simple_patch(group) for group in GROUP_ROWS}
    if merge:
        answers["merge"] = merge_answer(snapshot)
    return answers


def artifact(tmp_path) -> Path:
    path = tmp_path / ".paircli" / "pr-42" / HEAD
    path.mkdir(parents=True)
    return path


def run(tmp_path, stub, snapshot=None, draft=None, directory=None, **kwargs):
    if snapshot is None:
        snapshot, draft = world()
    directory = directory or artifact(tmp_path)
    log = io.StringIO()
    kwargs.setdefault("skeleton", SCORES)
    kwargs.setdefault("harness", "codex")
    kwargs.setdefault("min_call_seconds", 0)
    result = llm.generate_v2(directory, snapshot, HEAD, draft, stub, log, **kwargs)
    result["log"] = log.getvalue()
    result["dir"] = directory
    return result


def diag(directory: Path) -> Path:
    return directory / "diagnostics" / "architecture-llm"


def rows_of(document):
    return {node["id"]: node for node in document["nodes"]}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("PAIRCLI_ARCH_LLM_GROUPS", "PAIRCLI_ARCH_CONCURRENCY", "PAIRCLI_ARCH_CALL_TIMEOUT",
                 "PAIRCLI_ARCH_DEADLINE", "PAIRCLI_MODEL_GROUP", "PAIRCLI_MODEL_MERGE"):
        monkeypatch.delenv(name, raising=False)


# --- digests and prompts -----------------------------------------------------------------

def big_world():
    snapshot = make_snapshot(many_files(253))
    draft = stub_draft(snapshot, depth=2, per_group=60)
    return snapshot, draft


def test_group_digest_lists_rows_stats_edges_and_sensitive_flags():
    snapshot = make_snapshot([
        make_file("src/svc/token.ts", added=source_lines(3), removed=["old"], start=10),
        make_file("src/svc/plain.ts", added=source_lines(2), start=1),
        make_file("src/db/store.ts", added=source_lines(2), start=1),
    ])
    draft = stub_draft(snapshot, name=lambda key: key.rsplit("/", 1)[-1],
                       edges=[("e-svc-db", "src/svc/token.ts", "src/db/store.ts", 0)])
    digest = llm.build_group_digest(draft, "svc", snapshot)
    rows = {row["id"]: row for row in digest["rows"]}
    assert set(rows) == {"src-svc-token-ts", "src-svc-plain-ts"}
    token = rows["src-svc-token-ts"]
    assert token["sensitive"] is True and rows["src-svc-plain-ts"]["sensitive"] is False
    assert token["stats"] == {"files": 1, "additions": 3, "deletions": 1, "api_added": 0, "api_removed": 0}
    assert token["files"][0]["new_hunks"] == [[10, 14]] and token["files"][0]["old_hunks"] == [[10, 12]]
    edge = digest["candidate_edges"][0]
    assert edge["id"] == "e-svc-db" and edge["cross_group"] is True
    assert digest["other_rows"] == [{"id": "src-db-store-ts", "label": "store.ts", "group": "db"}]
    other = llm.build_group_digest(draft, "db", snapshot)
    assert [item["id"] for item in other["candidate_edges"]] == ["e-svc-db"]
    with pytest.raises(ValueError):
        llm.build_group_digest(draft, "missing", snapshot)


def test_group_digest_marks_files_without_line_data():
    snapshot = make_snapshot([make_file("src/a/x.ts", no_hunks=True), make_file("src/a/y.ts", added=["z"])])
    draft = stub_draft(snapshot)
    digest = llm.build_group_digest(draft, "src-a", snapshot)
    flags = {entry["path"]: entry["no_line_data"] for row in digest["rows"] for entry in row["files"]}
    assert flags == {"src/a/x.ts": True, "src/a/y.ts": False}


def test_group_digest_is_size_capped_for_a_large_group():
    snapshot = make_snapshot([
        make_file(f"src/big/part{index}/file{index}.ts", added=source_lines(30), removed=source_lines(5, "old"),
                  hunks=None) for index in range(60)])
    draft = stub_draft(snapshot, depth=2, per_group=60)
    digest = llm.build_group_digest(draft, "src-big", snapshot)
    assert llm.size_of(digest) <= llm.MAX_DIGEST_BYTES
    assert len(digest["rows"]) == 60


def test_group_prompt_is_small_references_paths_and_inlines_no_snapshot_text(tmp_path):
    snapshot, draft = big_world()
    group = draft["groups"][0]
    digest_path = tmp_path / "diagnostics" / "architecture-llm" / f"group-{group['id']}-input.json"
    prompt = llm.group_prompt(tmp_path, group, digest_path)
    assert len(prompt.encode()) < 12 * 1024
    assert str(digest_path) in prompt and str(tmp_path / "snapshot.json") in prompt
    for forbidden in ("const value", "old0 =", "context before", "packages/pkg"):
        assert forbidden not in prompt
    assert f'Group: id "{group["id"]}"' in prompt


def test_group_prompt_states_the_id_and_shape_rules(tmp_path):
    prompt = llm.group_prompt(tmp_path, {"id": "api", "label": "API"}, tmp_path / "d.json")
    for expected in ("copy row ids and candidate edge ids exactly", "Never invent ids", "at most 10",
                     "edges without an \"id\" are NEW edges", "side \"old\"", "side \"new\"",
                     "the single word null", "untrusted data", "Do not modify files", "expose secrets",
                     "Return only one JSON object", "cannot add, remove or rename rows",
                     "at most 80", "at most 300", "at most 400", "no_line_data"):
        assert expected in prompt, expected
    for kind in sorted(llm.NODE_KINDS - {"routine"}):
        assert kind in prompt


def test_retry_prompt_lists_bounded_error_lines_and_stays_small(tmp_path):
    errors = [{"path": f"edges[{index}].evidence[0].start", "reason": "x" * 600} for index in range(40)]
    prompt = llm.group_prompt(tmp_path, {"id": "api", "label": "API"}, tmp_path / "d.json", errors)
    lines = [line for line in prompt.splitlines() if line.startswith("edges[")]
    assert len(lines) == llm.MAX_RETRY_ERRORS and all(len(line) <= llm.MAX_RETRY_ERROR_CHARS for line in lines)
    assert "path: reason" in prompt and len(prompt.encode()) < 12 * 1024


def test_merge_prompt_mirrors_the_step_rules(tmp_path):
    prompt = llm.merge_prompt(tmp_path, tmp_path / "merge-input.json")
    assert str(tmp_path / "merge-input.json") in prompt and str(tmp_path / "snapshot.json") in prompt
    for expected in ("at most 24", "1-16 row ids", "never uses a removed", "never uses an added",
                     "Omit flags", "at most 400", "both of its endpoints must be in the step's nodes",
                     "the single word null", "untrusted data", "Do not modify files", "at most 2 generic limits"):
        assert expected in prompt, expected
    assert len(prompt.encode()) < 12 * 1024


GROUNDING_PHRASES = (
    "Base every row description, edge claim and step text on the cited lines",
    "PR title and description may be wrong or stale",
    "Mention only identifiers, flags, paths and options that you can see in the cited code; no counts",
    "only if the digest lists an edge from X to Y or the cited lines show",
    "without asserting a link between them",
    "old-side",
    "say less rather than guess",
)


def test_group_prompt_grounds_text_in_the_cited_code(tmp_path):
    prompt = " ".join(llm.group_prompt(tmp_path, {"id": "api", "label": "API"}, tmp_path / "d.json").split())
    for expected in GROUNDING_PHRASES:
        assert expected in prompt, expected
    assert "never infer the old behavior from added lines" in prompt
    assert len(prompt.encode()) < 12 * 1024


def test_merge_prompt_grounds_text_and_requires_old_side_support_for_before_steps(tmp_path):
    prompt = " ".join(llm.merge_prompt(tmp_path, tmp_path / "merge-input.json").split())
    for expected in GROUNDING_PHRASES:
        assert expected in prompt, expected
    assert 'Every "before" step must be supported by old-side citations' in prompt
    assert 'a "before" step may not cite added lines' in prompt
    assert "pr_title is only a hint and may be wrong" in prompt
    assert len(prompt.encode()) < 12 * 1024


def test_retry_prompts_keep_the_grounding_rules_and_stay_small(tmp_path):
    errors = [{"path": f"steps[{index}].evidence", "reason": "y" * 600} for index in range(40)]
    for prompt in (llm.group_prompt(tmp_path, {"id": "api", "label": "API"}, tmp_path / "d.json", errors),
                   llm.merge_prompt(tmp_path, tmp_path / "m.json", errors)):
        assert GROUNDING_PHRASES[0] in " ".join(prompt.split()) and len(prompt.encode()) < 12 * 1024


def test_the_prompt_version_changed_so_older_cached_patches_are_not_reused():
    assert llm.PROMPT_VERSION != "architecture-llm-1"


def test_merge_digest_lists_groups_rows_and_edges():
    snapshot, draft = world()
    digest = llm.build_merge_digest(draft, {"api": "API summary"}, snapshot)
    assert digest["pr_title"] == "Synthetic change"
    groups = {group["id"]: group for group in digest["groups"]}
    assert groups["api"]["summary"] == "API summary" and groups["db"]["summary"] == ""
    assert {row["id"] for row in groups["api"]["rows"]} == set(GROUP_ROWS["api"])
    edges = {edge["id"]: edge for edge in digest["edges"]}
    assert edges["imp-reviews-store"]["cross_group"] is True and edges["imp-reviews-session"]["cross_group"] is False
    assert llm.size_of(digest) <= llm.MAX_MERGE_DIGEST_BYTES


# --- pattern rows, API stats and the usefulness rules ------------------------------------

PATTERN_MEMBERS = [f"src/legacy/area{index}/module{index}.ts" for index in range(20)]


def pattern_world(stats=None, count=20, description="Removes the legacy module.", exports=0):
    """A draft with one file row and one pattern row of 20 members (the shape of the v2 contract).

    Each member deletes two plain lines and `exports` exported functions. `stats`, when given, is put on the
    row as the draft's own (the digests must ignore it: they read the computed values).
    """
    snapshot = make_snapshot([make_file(path, "deleted", start=1, removed=source_lines(2) + [
                                  f"export function gone{index}x{number}() {{}}" for number in range(exports)])
                              for index, path in enumerate(PATTERN_MEMBERS)]
                             + [make_file("src/legacy/index.ts", "modified", added=source_lines(2),
                                          removed=["export const old = 1;"], start=3)])
    draft = stub_draft(snapshot, depth=2, per_group=30)
    group = draft["nodes"][0]["group"]
    draft["nodes"] = [node for node in draft["nodes"] if node["files"] == ["src/legacy/index.ts"]]
    row = {"id": "legacy-deleted", "label": "20 files: deleted", "group": group, "kind": "pattern",
           "description": description, "files": PATTERN_MEMBERS[:3],
           "evidence": [cite(snapshot, path) for path in PATTERN_MEMBERS[:3]],
           "pattern": {"family": "same-change", "count": count, "signature": "9f2c4a1b7e3d5a60",
                       "members": PATTERN_MEMBERS}}
    if stats is not None:
        row["stats"] = stats
    draft["nodes"].append(row)
    return snapshot, draft, group


def test_group_digest_carries_the_pattern_family_count_signature_samples_and_stats():
    stats = {"files": 20, "additions": 0, "deletions": 40, "api_added": 0, "api_removed": 0}
    snapshot, draft, group = pattern_world(stats)
    digest = llm.build_group_digest(draft, group, snapshot)
    rows = {row["id"]: row for row in digest["rows"]}
    assert set(rows) == {"src-legacy-index-ts", "legacy-deleted"}, "pattern rows are listed like file rows"
    row = rows["legacy-deleted"]
    assert row["kind"] == "pattern"
    assert row["pattern"] == {"family": "same-change", "count": 20, "signature": "9f2c4a1b7e3d5a60",
                              "samples": PATTERN_MEMBERS[:llm.MAX_PATTERN_SAMPLES],
                              "directories": 20, "common_dir": "src/legacy"}
    assert len(row["pattern"]["samples"]) == 12
    assert row["stats"] == stats
    assert [entry["path"] for entry in row["files"]] == PATTERN_MEMBERS[:3]
    assert "pattern" not in rows["src-legacy-index-ts"]


def test_pattern_row_without_stats_reports_computed_totals_over_all_members_not_the_sample():
    snapshot, draft, group = pattern_world()
    row = next(row for row in llm.build_group_digest(draft, group, snapshot)["rows"] if row["kind"] == "pattern")
    assert row["stats"] == {"files": 20, "additions": 0, "deletions": 40, "api_added": 0, "api_removed": 0}


def test_digest_stats_are_computed_and_ignore_numbers_written_on_the_draft():
    snapshot, draft, group = pattern_world({"files": 3, "additions": 99, "deletions": 99, "api_added": 9, "api_removed": 9},
                                           exports=2)
    row = next(row for row in llm.build_group_digest(draft, group, snapshot)["rows"] if row["kind"] == "pattern")
    assert row["stats"] == {"files": 20, "additions": 0, "deletions": 80, "api_added": 0, "api_removed": 40}
    assert draft["nodes"][-1]["stats"]["api_removed"] == 9, "the draft is not mutated"


def test_a_deletion_heavy_pattern_row_that_removes_80_exports_reaches_both_digests():
    snapshot, draft, group = pattern_world(exports=4, description="Deletes the legacy modules; callers outside this PR may break.")
    before = copy.deepcopy(draft)
    row = next(row for row in llm.build_group_digest(draft, group, snapshot)["rows"] if row["kind"] == "pattern")
    assert row["stats"]["api_removed"] == 80 and row["stats"]["api_added"] == 0
    other = next(row for row in llm.build_group_digest(draft, group, snapshot)["rows"] if row["kind"] == "file")
    assert other["stats"]["api_removed"] == 1, "export const old = 1; leaves the index"
    digest = llm.build_merge_digest(draft, {}, snapshot)
    rows = {row["id"]: row for row in digest["groups"][0]["rows"]}
    assert rows["legacy-deleted"]["api_removed"] == 80 and rows["src-legacy-index-ts"]["api_removed"] == 1
    assert digest["groups"][0]["api_removed"] == 81 and digest["api"]["removed"] == 81 and digest["api"]["added"] == 0
    breaking = {item["id"]: item["api_removed"] for item in digest["api"]["breaking_rows"]}
    assert breaking == {"legacy-deleted": 80, "src-legacy-index-ts": 1}
    assert draft == before


def test_a_deterministic_draft_with_exports_removed_feeds_the_digests():
    from paircli_architecture_v2_draft import deterministic_v2_draft
    from paircli_skeleton import build_skeleton
    files = [make_file(f"src/legacy/t{index}.ts", "deleted", start=1,
                       removed=[f"export function a{index}() {{}}", f"export function b{index}() {{}}"]) for index in range(40)]
    snapshot = make_snapshot(files)
    draft = deterministic_v2_draft(build_skeleton(snapshot, HEAD), snapshot, HEAD)
    assert [node["kind"] for node in draft["nodes"]] == ["pattern"] and "stats" not in draft["nodes"][0]
    group = draft["groups"][0]["id"]
    assert llm.build_group_digest(draft, group, snapshot)["rows"][0]["stats"]["api_removed"] == 80
    assert llm.build_merge_digest(draft, {}, snapshot)["api"]["removed"] == 80


def test_group_digest_tolerates_malformed_pattern_and_stats_values():
    snapshot, draft, group = pattern_world({"files": "many", "api_removed": True, "api_added": -1})
    draft["nodes"][-1]["pattern"] = {"family": "docs", "count": "x", "members": [1, None]}
    row = next(row for row in llm.build_group_digest(draft, group, snapshot)["rows"] if row["kind"] == "pattern")
    assert row["pattern"]["samples"] == PATTERN_MEMBERS[:3] and row["pattern"]["count"] == 3
    assert row["stats"]["api_removed"] == 0 and row["stats"]["api_added"] == 0, "junk draft stats never reach the digest"
    draft["nodes"][-1]["pattern"] = "broken"
    assert "pattern" not in next(row for row in llm.build_group_digest(draft, group, snapshot)["rows"]
                                 if row["kind"] == "pattern")


def test_group_digest_of_a_draft_without_patterns_is_unchanged():
    snapshot, draft = world()
    digest = llm.build_group_digest(draft, "api", snapshot)
    for row in digest["rows"]:
        assert "pattern" not in row
        assert set(row["stats"]) == {"files", "additions", "deletions", "api_added", "api_removed"}
    draft["nodes"][0]["stats"] = {"files": 1, "additions": 3, "deletions": 1, "api_removed": 2, "api_added": 0}
    row = next(row for row in llm.build_group_digest(draft, "api", snapshot)["rows"] if row["id"] == draft["nodes"][0]["id"])
    assert row["stats"]["api_removed"] == 0, "computed from the snapshot, not read from the draft"


def test_group_digest_with_many_pattern_rows_stays_size_capped():
    snapshot, draft, group = pattern_world()
    template = draft["nodes"][-1]
    for index in range(60):
        row = copy.deepcopy(template)
        row["id"] = f"legacy-deleted-{index}"
        row["pattern"]["members"] = [f"src/legacy/long/{'d' * 40}/{index}/member{number}.ts" for number in range(500)]
        row["pattern"]["count"] = 500
        draft["nodes"].append(row)
    digest = llm.build_group_digest(draft, group, snapshot)
    assert llm.size_of(digest) <= llm.MAX_DIGEST_BYTES and len(digest["rows"]) == 62


def test_merge_digest_carries_pattern_api_totals_and_breaking_rows():
    snapshot, draft, group = pattern_world(exports=1, description="Deletes the legacy module; callers outside this PR may break.")
    digest = llm.build_merge_digest(draft, {}, snapshot)
    rows = {row["id"]: row for row in digest["groups"][0]["rows"]}
    assert rows["legacy-deleted"]["pattern"] == {"family": "same-change", "count": 20, "directories": 20,
                                                  "common_dir": "src/legacy"}
    assert rows["legacy-deleted"]["stats"]["files"] == 20, "the true count sits next to the row"
    assert rows["legacy-deleted"]["api_removed"] == 20 and rows["src-legacy-index-ts"]["api_removed"] == 1
    assert digest["groups"][0]["api_removed"] == 21 and "api_added" in digest["groups"][0]
    assert digest["api"]["removed"] == 21 and digest["api"]["added"] == 0
    assert {row["id"] for row in digest["api"]["breaking_rows"]} == {"legacy-deleted", "src-legacy-index-ts"}
    assert "legacy module" in rows["legacy-deleted"]["description"]
    assert llm.size_of(digest) <= llm.MAX_MERGE_DIGEST_BYTES


def test_merge_digest_lists_a_row_whose_description_mentions_a_break_even_without_numbers():
    snapshot, draft = world()
    draft["nodes"][0]["description"] = "Removes the old export, so callers outside this PR may break."
    digest = llm.build_merge_digest(draft, {}, snapshot)
    assert digest["api"] == {"removed": 0, "added": 0,
                             "breaking_rows": [{"id": draft["nodes"][0]["id"], "api_removed": 0,
                                                "description": draft["nodes"][0]["description"]}]}


def test_merge_digest_of_a_pr_without_api_changes_has_no_api_block():
    snapshot, draft = world()
    digest = llm.build_merge_digest(draft, {}, snapshot)
    assert "api" not in digest
    assert all("api_removed" not in group and "pattern" not in row for group in digest["groups"] for row in group["rows"])


def test_merge_digest_is_size_capped_with_many_rows_and_long_descriptions():
    snapshot, draft = big_world()
    for node in draft["nodes"]:
        node["description"] = "x" * 300
    digest = llm.build_merge_digest(draft, {}, snapshot)
    assert llm.size_of(digest) <= llm.MAX_MERGE_DIGEST_BYTES
    assert all(len(row["description"]) <= llm.MERGE_DESCRIPTION_CHARS for group in digest["groups"] for row in group["rows"])


USEFULNESS_PHRASES = (
    "Lead with what the change does or breaks, never with line counts",
    "describe the shared change once, with one concrete line from a sample member you read",
    'write "removes N exported names; callers outside this PR may break" only when api_removed is above 0',
    "an identifier used but not defined in the changed code",
    "code after return or throw that can never run",
    "an import of a name the target does not export",
    "never as a guess",
    "If unsure, omit it",
    "Never add an edge to or from a tests, docs or config row",
    "only because one tests the other",
    'write "covered by tests in <file>" in the tested row\'s description instead',
    "A new edge needs a real runtime call, data flow or import that the cited lines show",
    "repository checkout",
)


def test_both_prompts_carry_the_usefulness_rules_and_the_grounding_block(tmp_path):
    prompts = (llm.group_prompt(tmp_path, {"id": "api", "label": "API"}, tmp_path / "d.json"),
               llm.merge_prompt(tmp_path, tmp_path / "m.json"))
    for prompt in prompts:
        flat = " ".join(prompt.split())
        for expected in USEFULNESS_PHRASES + GROUNDING_PHRASES:
            assert expected in flat, expected


def test_group_prompt_keeps_pattern_rows_as_they_are(tmp_path):
    flat = " ".join(llm.group_prompt(tmp_path, {"id": "api", "label": "API"}, tmp_path / "d.json").split())
    assert "A row whose kind is routine or pattern keeps it" in flat


def test_merge_prompt_asks_for_three_to_eight_steps_that_show_a_path_or_a_contrast(tmp_path):
    flat = " ".join(llm.merge_prompt(tmp_path, tmp_path / "m.json").split())
    for expected in ("3 to 8", "at most 24", "path across at least two rows", "before/after contrast of one row's behaviour",
                     "No step may only restate a row, only say tests or docs were added, or only list files",
                     "main behaviour change first and risks last", "api.removed is above 0", "breaking API change"):
        assert expected in flat, expected


def test_prompts_fit_the_size_limit_for_a_253_file_digest_even_on_retry(tmp_path):
    snapshot, draft = big_world()
    digest = llm.build_group_digest(draft, draft["groups"][0]["id"], snapshot)
    assert llm.size_of(digest) <= llm.MAX_DIGEST_BYTES
    deep = tmp_path.joinpath(*(["a-long-folder-name"] * 6)) / "diagnostics" / "architecture-llm"
    errors = [{"path": f"steps[{index}].evidence", "reason": "z" * 600} for index in range(40)]
    for given in (None, errors):
        group = llm.group_prompt(deep, draft["groups"][0], deep / "group-x-input.json", given)
        merge = llm.merge_prompt(deep, deep / "merge-input.json", given)
        assert len(group.encode()) < 12 * 1024 and len(merge.encode()) < 12 * 1024


def test_the_prompt_version_changed_and_cache_keys_differ(monkeypatch):
    assert llm.PROMPT_VERSION == "architecture-llm-4"
    snapshot, draft = world()
    digest = llm.build_merge_digest(draft, {}, snapshot)
    group_key = llm.group_cache_key(draft, "api", snapshot, "codex", None)
    merge_key = llm.merge_cache_key(digest, [], snapshot, "codex", None)
    monkeypatch.setattr(llm, "PROMPT_VERSION", "architecture-llm-2")
    assert llm.group_cache_key(draft, "api", snapshot, "codex", None) != group_key
    assert llm.merge_cache_key(digest, [], snapshot, "codex", None) != merge_key


def test_pattern_rows_are_editable_but_keep_their_kind(monkeypatch):
    # The validator learns the pattern kind in a separate change; here only this module's checks are exercised.
    monkeypatch.setattr(llm, "finalize_v2", lambda document, head, snapshot, generated_by: (document, []))
    snapshot, draft, group = pattern_world()
    patch = {"rows": [{"id": "legacy-deleted", "label": "20 legacy modules deleted", "kind": "service",
                       "description": "Deletes 20 legacy modules; each file removes `export const old = 1;`."}]}
    clean, new, warnings = llm.check_group_patch(draft, group, patch, HEAD, snapshot)
    assert clean["rows"][0]["label"] == "20 legacy modules deleted" and "kind" not in clean["rows"][0]
    assert any("pattern rows keep kind pattern" in warning for warning in warnings)
    row = rows_of(new)["legacy-deleted"]
    assert row["kind"] == "pattern" and row["description"].startswith("Deletes 20 legacy modules")
    assert row["pattern"] == draft["nodes"][-1]["pattern"] and row["files"] == PATTERN_MEMBERS[:3]
    clean, _, warnings = llm.check_group_patch(draft, group, {"rows": [{"id": "legacy-deleted", "kind": "pattern"}]},
                                               HEAD, snapshot)
    assert clean["rows"] == [] and warnings == []
    clean, _, warnings = llm.check_group_patch(draft, group, {"rows": [{"id": "src-legacy-index-ts", "kind": "pattern"}]},
                                               HEAD, snapshot)
    assert clean["rows"] == [] and any("cannot become pattern" in warning for warning in warnings)


# --- applying a group patch --------------------------------------------------------------

def apply(draft, snapshot, patch, group="api"):
    return llm.apply_group_patch(draft, group, patch, HEAD, snapshot)


def test_text_fields_of_existing_rows_and_edges_are_applied():
    snapshot, draft = world()
    new, warnings = apply(draft, snapshot, api_patch(snapshot))
    row = rows_of(new)[ROW[REVIEWS]]
    assert (row["label"], row["description"], row["kind"]) == ("Reviews API", "Validates a review before saving it.", "service")
    edge = next(edge for edge in new["edges"] if edge["id"] == "imp-reviews-store")
    assert (edge["label"], edge["claim"]) == ("saves", "The handler now calls the store.")
    assert new["nodes"][0]["files"] == draft["nodes"][0]["files"]
    assert draft == world()[1], "the input draft must not be mutated"
    assert warnings == []
    finalize_v2(new, HEAD, snapshot, "llm")


def test_patch_may_be_raw_text_or_fenced():
    snapshot, draft = world()
    text = json.dumps(api_patch(snapshot))
    assert apply(draft, snapshot, text)[0] == apply(draft, snapshot, api_patch(snapshot))[0]
    assert apply(draft, snapshot, f"```json\n{text}\n```")[0] == apply(draft, snapshot, text)[0]


def test_unknown_ids_are_ignored_with_a_warning():
    snapshot, draft = world()
    patch = api_patch(snapshot)
    patch["rows"].append({"id": "ghost", "label": "Ghost"})
    patch["edges"].append({"id": "ghost-edge", "label": "x", "claim": "y"})
    new, warnings = apply(draft, snapshot, patch)
    assert "ghost" not in rows_of(new) and len(new["nodes"]) == len(draft["nodes"])
    assert len(new["edges"]) == len(draft["edges"])
    assert any("unknown row id" in warning for warning in warnings)
    assert any("unknown candidate edge id" in warning for warning in warnings)


def test_rows_of_another_group_are_ignored():
    snapshot, draft = world()
    new, warnings = apply(draft, snapshot, {"rows": [{"id": ROW[STORE], "label": "Hijacked"}]})
    assert rows_of(new)[ROW[STORE]]["label"] == "store.ts"
    assert any("belongs to another group" in warning for warning in warnings)


def test_unknown_keys_are_stripped_with_warnings():
    snapshot, draft = world()
    patch = api_patch(snapshot, nodes=[{"id": "extra"}], groups=[], title="Hacked")
    patch["rows"][0]["color"] = "red"
    patch["edges"][0]["style"] = "bold"
    new, warnings = apply(draft, snapshot, patch)
    assert new["title"] == draft["title"] and len(new["nodes"]) == len(draft["nodes"])
    assert any("nodes: removed unexpected key" in warning for warning in warnings)
    assert any("color: not editable" in warning for warning in warnings)
    assert any("style: not editable" in warning for warning in warnings)


def test_attempts_to_add_rows_or_files_are_ignored():
    snapshot, draft = world()
    patch = {"rows": [{"id": "brand-new", "label": "New", "files": [STORE], "group": "api"},
                      {"id": ROW[REVIEWS], "label": "Fine", "files": ["src/evil.ts"], "group": "db"}]}
    new, warnings = apply(draft, snapshot, patch)
    assert [node["id"] for node in new["nodes"]] == [node["id"] for node in draft["nodes"]]
    row = rows_of(new)[ROW[REVIEWS]]
    assert row["label"] == "Fine" and row["files"] == [REVIEWS] and row["group"] == "api"
    assert [group["id"] for group in new["groups"]] == [group["id"] for group in draft["groups"]]


def test_computed_fields_cannot_be_overridden():
    snapshot, draft = world()
    patch = {"rows": [{"id": ROW[SESSION], "label": "Session", "change": "removed", "state": "context",
                       "stats": {"files": 9, "additions": 99, "deletions": 99}}],
             "edges": [{"id": "imp-reviews-store", "label": "uses", "claim": "Uses the store.", "change": "removed",
                        "source": ROW[STORE], "target": ROW[REVIEWS], "evidence": []}]}
    new, warnings = apply(draft, snapshot, patch)
    document, _ = finalize_v2(new, HEAD, snapshot, "llm")
    row = rows_of(document)[ROW[SESSION]]
    assert row["change"] == "added" and row["state"] == "changed" and row["stats"]["additions"] == 5
    edge = next(edge for edge in document["edges"] if edge["id"] == "imp-reviews-store")
    assert (edge["source"], edge["target"]) == (ROW[REVIEWS], ROW[STORE]) and edge["change"] == "added"
    assert edge["evidence"] == draft["edges"][0]["evidence"]
    assert any("not editable" in warning for warning in warnings)


@pytest.mark.parametrize("kind, row, accepted", [
    ("database", ROW[STORE], True), ("routine", ROW[STORE], False), ("service", "root-routine", False),
])
def test_routine_kind_is_locked(kind, row, accepted):
    snapshot, draft = world()
    group = next(node["group"] for node in draft["nodes"] if node["id"] == row)
    new, warnings = apply(draft, snapshot, {"rows": [{"id": row, "kind": kind}]}, group)
    assert (rows_of(new)[row]["kind"] == kind) is accepted
    assert bool(warnings) is (not accepted)


def test_new_edge_with_a_valid_new_side_citation_is_added():
    snapshot, draft = world()
    patch = {"edges": [{"source": ROW[SESSION], "target": ROW[STORE], "label": "stores", "claim": "Session data is saved.",
                        "evidence": [cite(snapshot, SESSION, 1)]}]}
    new, warnings = apply(draft, snapshot, patch)
    added = [edge for edge in new["edges"] if edge not in draft["edges"]]
    assert len(added) == 1 and added[0]["id"].startswith("e-") and added[0]["evidence"] == [cite(snapshot, SESSION, 1)]
    document, _ = finalize_v2(new, HEAD, snapshot, "llm")
    assert next(edge for edge in document["edges"] if edge["id"] == added[0]["id"])["change"] == "added"


def test_new_edge_may_cite_the_old_side():
    snapshot, draft = world()
    old = cite(snapshot, REVIEWS, 1, side="old")
    patch = {"edges": [{"source": ROW[REVIEWS], "target": ROW[LEGACY], "label": "used", "claim": "Used the legacy file.",
                        "evidence": [old]}]}
    new, _ = apply(draft, snapshot, patch)
    document, _ = finalize_v2(new, HEAD, snapshot, "llm")
    assert document["edges"][-1]["change"] == "removed" and document["edges"][-1]["evidence"] == [old]


def test_new_edge_with_an_invalid_citation_is_rejected_with_path_and_reason():
    snapshot, draft = world()
    bad = dict(cite(snapshot, SESSION), start=500, end=501)
    patch = {"edges": [{"source": ROW[SESSION], "target": ROW[STORE], "label": "x", "claim": "y", "evidence": [bad]}]}
    with pytest.raises(ArchitectureError) as caught:
        apply(draft, snapshot, patch)
    assert any(error["path"] == "edges[0].evidence[0].start" and "hunk" in error["reason"] for error in caught.value.errors)


def test_new_edge_old_side_citation_of_an_added_file_is_invalid():
    snapshot, draft = world()
    bad = dict(cite(snapshot, SESSION), side="old")
    patch = {"edges": [{"source": ROW[SESSION], "target": ROW[STORE], "label": "x", "claim": "y", "evidence": [bad]}]}
    with pytest.raises(ArchitectureError):
        apply(draft, snapshot, patch)


def test_invalid_citations_are_dropped_when_a_valid_one_remains():
    snapshot, draft = world()
    bad = dict(cite(snapshot, SESSION), start=500, end=501)
    patch = {"edges": [{"source": ROW[SESSION], "target": ROW[STORE], "label": "x", "claim": "y",
                        "evidence": [bad, cite(snapshot, SESSION, 2), {"path": SESSION, "start": 1, "end": 1}]}]}
    new, warnings = apply(draft, snapshot, patch)
    # the citation without type or side gets the defaults and is valid; the out-of-range one is dropped
    assert new["edges"][-1]["evidence"] == [cite(snapshot, SESSION, 2), cite(snapshot, SESSION, 0)]
    assert any("citation dropped" in warning for warning in warnings)


@pytest.mark.parametrize("edge", [
    {"source": "nope", "target": ROW[STORE]},
    {"source": ROW[STORE], "target": "nope"},
    {"source": ROW[STORE], "target": ROW[STORE]},
])
def test_new_edge_needs_two_distinct_existing_rows(edge):
    snapshot, draft = world()
    patch = {"edges": [{**edge, "label": "x", "claim": "y", "evidence": [cite(snapshot, STORE)]}]}
    with pytest.raises(ArchitectureError):
        apply(draft, snapshot, patch, "db")


def test_new_edge_without_evidence_is_rejected():
    snapshot, draft = world()
    with pytest.raises(ArchitectureError) as caught:
        apply(draft, snapshot, {"edges": [{"source": ROW[SESSION], "target": ROW[STORE], "label": "x", "claim": "y"}]})
    assert any("at least one valid diff citation" in error["reason"] for error in caught.value.errors) or \
        any(error["path"].endswith("evidence") for error in caught.value.errors)


def test_new_edge_may_reach_any_group():
    snapshot, draft = world()
    patch = {"edges": [{"source": ROW[SESSION], "target": ROW[NAMES], "label": "x", "claim": "Uses names.",
                        "evidence": [cite(snapshot, SESSION)]}]}
    new, _ = apply(draft, snapshot, patch)
    assert len(new["edges"]) == len(draft["edges"]) + 1


def test_duplicate_pair_edges_are_ignored():
    snapshot, draft = world()
    patch = {"edges": [{"source": ROW[REVIEWS], "target": ROW[STORE], "label": "x", "claim": "y",
                        "evidence": [cite(snapshot, REVIEWS)]}]}
    new, warnings = apply(draft, snapshot, patch)
    assert len(new["edges"]) == len(draft["edges"]) and any("already exists" in warning for warning in warnings)


def test_unknown_edge_id_with_endpoints_is_treated_as_a_new_edge():
    snapshot, draft = world()
    patch = {"edges": [{"id": "new1", "source": ROW[SESSION], "target": ROW[STORE], "label": "x", "claim": "y",
                        "evidence": [cite(snapshot, SESSION)]}]}
    new, warnings = apply(draft, snapshot, patch)
    assert len(new["edges"]) == len(draft["edges"]) + 1 and new["edges"][-1]["id"] != "new1"
    assert any("treated as a new edge" in warning for warning in warnings)


def crowded(files=12, edges=0):
    snapshot = make_snapshot([make_file(f"src/pkg/file{index:02}.ts", added=source_lines(4), start=1) for index in range(files)])
    pairs = [(a, b) for a in range(files) for b in range(files) if a != b]
    names = [f"src/pkg/file{index:02}.ts" for index in range(files)]
    draft = stub_draft(snapshot, per_group=files,
                       edges=[(f"e{n}", names[a], names[b], 0) for n, (a, b) in enumerate(pairs[:edges])])
    return snapshot, draft, names


def new_edge_between(snapshot, names, a, b):
    return {"source": rid(names[a]), "target": rid(names[b]), "label": "uses", "claim": "Uses it.",
            "evidence": [cite(snapshot, names[a])]}


def test_new_edges_are_capped_per_group():
    snapshot, draft, names = crowded()
    patch = {"edges": [new_edge_between(snapshot, names, 0, other) for other in range(1, 12)] +
                      [new_edge_between(snapshot, names, 1, 2), new_edge_between(snapshot, names, 1, 3)]}
    new, warnings = apply(draft, snapshot, patch, "src-pkg")
    assert len(new["edges"]) == llm.MAX_NEW_EDGES_PER_GROUP
    assert any("more than 10 new edges" in warning for warning in warnings)


def test_total_edge_cap_is_enforced():
    snapshot, draft, names = crowded(edges=57)
    patch = {"edges": [new_edge_between(snapshot, names, 11, other) for other in range(0, 6)]}
    new, warnings = apply(draft, snapshot, patch, "src-pkg")
    assert len(new["edges"]) == 60 and any("edge limit" in warning for warning in warnings)
    finalize_v2(new, HEAD, snapshot, "llm")


@pytest.mark.parametrize("patch, path", [
    ({"rows": [{"id": ROW[REVIEWS], "label": "x" * 81}]}, "rows[0].label"),
    ({"rows": [{"id": ROW[REVIEWS], "description": "x" * 301}]}, "rows[0].description"),
    ({"rows": [{"id": ROW[REVIEWS], "label": ""}]}, "rows[0].label"),
    ({"rows": [{"id": ROW[REVIEWS], "label": 5}]}, "rows[0].label"),
    ({"rows": [{"id": ROW[REVIEWS], "kind": "spaceship"}]}, "rows[0].kind"),
    ({"rows": [{"id": ROW[REVIEWS], "label": "<b>bold</b>"}]}, "rows[0].label"),
    ({"edges": [{"id": "imp-reviews-store", "claim": "x" * 401}]}, "edges[0].claim"),
    ({"edges": [{"id": "imp-reviews-store", "label": "y" * 81}]}, "edges[0].label"),
    ({"rows": "not a list"}, "rows"),
    ({"edges": ["nope"]}, "edges[0]"),
])
def test_bad_text_is_a_structured_error_for_the_retry(patch, path):
    snapshot, draft = world()
    with pytest.raises(ArchitectureError) as caught:
        apply(draft, snapshot, patch)
    assert path in [error["path"] for error in caught.value.errors]


@pytest.mark.parametrize("raw", ["not json at all", "[1, 2]", '"text"', "{", ""])
def test_unparseable_or_non_object_output_is_an_error(raw):
    snapshot, draft = world()
    with pytest.raises(ArchitectureError) as caught:
        apply(draft, snapshot, raw)
    assert caught.value.errors[0]["path"] == "$"


def test_null_patch_returns_the_draft_unchanged():
    snapshot, draft = world()
    new, warnings = apply(draft, snapshot, "null")
    assert new == draft and any("null" in warning for warning in warnings)
    assert apply(draft, snapshot, None)[0] == draft


def test_summary_is_truncated_and_never_enters_the_draft():
    snapshot, draft = world()
    clean, new, _ = llm.check_group_patch(draft, "api", {"summary": "s" * 500}, HEAD, snapshot)
    assert len(clean["summary"]) == llm.SUMMARY_MAX and new == draft


def test_clean_patch_is_idempotent_so_cached_patches_can_be_rechecked():
    snapshot, draft = world()
    patch = api_patch(snapshot)
    patch["edges"].append({"source": ROW[SESSION], "target": ROW[STORE], "label": "x", "claim": "y",
                           "evidence": [cite(snapshot, SESSION)]})
    clean, new, _ = llm.check_group_patch(draft, "api", patch, HEAD, snapshot)
    again, newer, warnings = llm.check_group_patch(draft, "api", clean, HEAD, snapshot)
    assert again == clean and newer == new and warnings == []


# --- merge -------------------------------------------------------------------------------

def test_merge_adds_edges_title_gaps_and_valid_steps():
    snapshot, draft = world()
    new, warnings, dropped = llm.apply_merge(draft, merge_answer(snapshot), HEAD, snapshot)
    assert dropped == 0 and new["title"] == "Reviews are validated" and new["gaps"] == ["No tests were changed."]
    added = new["edges"][-1]
    assert added["id"] == "x-names-reviews" and (added["source"], added["target"]) == (ROW[NAMES], ROW[REVIEWS])
    assert [step["id"] for step in new["steps"]] == ["s1", "s2", "s3"]
    document, _ = finalize_v2(new, HEAD, snapshot, "llm")
    assert [step["id"] for step in document["steps"]] == ["s1", "s2", "s3"]
    assert all(step["flags"] == [] for step in document["steps"])


def test_merge_edge_ids_are_mapped_when_the_proposed_id_is_taken():
    snapshot, draft = world()
    answer = merge_answer(snapshot)
    answer["edges"][0]["id"] = "imp-reviews-store"
    new, _, dropped = llm.apply_merge(draft, answer, HEAD, snapshot)
    ids = [edge["id"] for edge in new["edges"]]
    assert len(ids) == len(set(ids)) and ids[-1] != "imp-reviews-store"
    assert dropped == 1, "s3 named an edge id that was not free, so its edge never existed"
    finalize_v2(new, HEAD, snapshot, "llm")


def test_invalid_steps_are_dropped_and_the_graph_is_kept():
    snapshot, draft = world()
    good = merge_answer(snapshot)["steps"][0]
    answer = merge_answer(snapshot, steps=[
        good,
        {**good, "id": "bad-node", "nodes": ["ghost"]},
        {**good, "id": "bad-long", "text": "x" * 401},
        {**good, "id": "bad-empty", "nodes": []},
        {**good, "id": "bad-edge", "edge": "ghost-edge"},
        {**good, "id": "bad-endpoint", "edge": "imp-reviews-store", "nodes": [ROW[REVIEWS]]},
        {**good, "id": "bad-evidence", "evidence": [dict(good["evidence"][0], start=900, end=901)]},
        {**good, "id": "bad-when", "when": "during"},
        {**good, "id": "s1"},
        "not an object",
    ])
    new, warnings, dropped = llm.apply_merge(draft, answer, HEAD, snapshot)
    assert [step["id"] for step in new["steps"]] == ["s1"] and dropped == 9
    assert len(new["edges"]) == len(draft["edges"]) + 1, "the graph is not dropped"
    assert any("bad-node dropped" in warning for warning in warnings)
    finalize_v2(new, HEAD, snapshot, "llm")


def test_after_step_on_removed_node_and_before_step_on_added_node_are_dropped():
    snapshot, draft = world()
    evidence = [cite(snapshot, REVIEWS, 0)]
    answer = merge_answer(snapshot, edges=[], steps=[
        {"id": "after-removed", "when": "after", "nodes": [ROW[LEGACY]], "text": "Legacy is gone.", "evidence": [cite(snapshot, LEGACY)]},
        {"id": "before-added", "when": "before", "nodes": [ROW[SESSION]], "text": "No session.", "evidence": [cite(snapshot, SESSION)]},
        {"id": "before-added-edge", "when": "before", "edge": "imp-reviews-store", "nodes": [ROW[REVIEWS], ROW[STORE]],
         "text": "Before.", "evidence": evidence},
        {"id": "before-removed", "when": "before", "nodes": [ROW[LEGACY]], "text": "Legacy exists.", "evidence": [cite(snapshot, LEGACY)]},
        {"id": "after-added", "when": "after", "nodes": [ROW[SESSION]], "text": "Session exists.", "evidence": [cite(snapshot, SESSION)]},
    ])
    new, _, dropped = llm.apply_merge(draft, answer, HEAD, snapshot)
    assert [step["id"] for step in new["steps"]] == ["before-removed", "after-added"] and dropped == 3


def test_merge_step_limit_and_flags_and_unknown_keys():
    snapshot, draft = world()
    template = merge_answer(snapshot)["steps"][0]
    steps = [{**template, "id": f"s{number}", "flags": ["x"], "color": "red"} for number in range(30)]
    new, warnings, dropped = llm.apply_merge(draft, {"steps": steps, "bogus": 1}, HEAD, snapshot)
    assert len(new["steps"]) == 24 and dropped == 6
    assert all("color" not in step and "flags" not in step for step in new["steps"])
    document, _ = finalize_v2(new, HEAD, snapshot, "llm")
    assert all(step["flags"] == [] for step in document["steps"])
    assert any("bogus: removed unexpected key" in warning for warning in warnings)


def test_merge_edges_are_validated_one_by_one():
    snapshot, draft = world()
    good = merge_answer(snapshot)["edges"][0]
    answer = merge_answer(snapshot, steps=[], edges=[
        good,
        {**good, "id": "e-bad-end", "target": "ghost"},
        {**good, "id": "e-bad-ref", "evidence": [dict(good["evidence"][0], start=900, end=900)]},
        {**good, "id": "e-bad-claim", "claim": "x" * 401},
        {**good, "id": "e-self", "target": good["source"]},
        {**good, "id": "e-dup"},
    ])
    new, warnings, _ = llm.apply_merge(draft, answer, HEAD, snapshot)
    assert [edge["id"] for edge in new["edges"][len(draft["edges"]):]] == ["x-names-reviews"]
    assert len(warnings) >= 4


def test_merge_null_and_non_object():
    snapshot, draft = world()
    new, warnings, dropped = llm.apply_merge(draft, "null", HEAD, snapshot)
    assert new == draft and dropped == 0 and warnings
    with pytest.raises(ArchitectureError):
        llm.apply_merge(draft, "[1]", HEAD, snapshot)
    with pytest.raises(ArchitectureError):
        llm.apply_merge(draft, "garbage", HEAD, snapshot)


def test_merge_ignores_bad_title_and_gaps_without_failing():
    snapshot, draft = world()
    answer = {"title": "x" * 200, "description": "", "gaps": ["ok", "x" * 301, 5, "ok", "a", "b", "c", "d", "e"]}
    new, warnings, _ = llm.apply_merge(draft, answer, HEAD, snapshot)
    assert new["title"] == draft["title"] and new["description"] == draft["description"]
    assert new["gaps"] == ["ok", "a", "b", "c", "d"]
    finalize_v2(new, HEAD, snapshot, "llm")


GENERIC_GAPS = ["Runtime behaviour is not visible in the diff.", "Callers outside this PR are not shown.", "Either and/or both."]
SPECIFIC_GAPS = ["area4 may not resolve", "src/zones/zone1 is not shown", "zone1.ts is not shown",
                 "zone1, zone3, zone4 not shown", "12 files are hidden", "3 more rows were collapsed",
                 "40 areas are not drawn", "Check config.json and package.lock", "See ./docs/readme.md"]


def test_merge_keeps_generic_gaps_and_drops_gaps_about_paths_files_or_counts():
    snapshot, draft = world()
    answer = {"gaps": SPECIFIC_GAPS[:4] + GENERIC_GAPS[:1] + SPECIFIC_GAPS[4:] + GENERIC_GAPS[1:]}
    new, warnings, _ = llm.apply_merge(draft, answer, HEAD, snapshot)
    assert new["gaps"] == GENERIC_GAPS
    dropped = [warning for warning in warnings if "only generic gaps are accepted" in warning]
    assert len(dropped) == len(SPECIFIC_GAPS) and all(warning.startswith("merge: gaps[") for warning in dropped)
    finalize_v2(new, HEAD, snapshot, "llm")


@pytest.mark.parametrize("gap", ["The 2 steps are examples.", "Version 2 of the flow.", "Runtime behaviour is not visible in the diff"])
def test_merge_gap_filter_does_not_reject_plain_numbers_in_generic_text(gap):
    snapshot, draft = world()
    new, _, _ = llm.apply_merge(draft, {"gaps": [gap]}, HEAD, snapshot)
    assert new["gaps"] == [gap]


def test_the_merge_prompt_limits_gaps_to_two_generic_limits(tmp_path):
    flat = " ".join(llm.merge_prompt(tmp_path, tmp_path / "m.json").split())
    for phrase in ("gaps: at most 2 generic limits of the diagram", "runtime behaviour not visible in the diff",
                   "callers outside this PR", "NEVER a statement about specific files, directories, areas, rows or counts",
                   "code adds its own computed gaps"):
        assert phrase in flat, phrase
    assert "at most 5 honest notes" not in flat


NEW_RULE_PHRASES = (
    'Do not use "only", "just", "all", "every" or "none" about what a file or route serves, calls or contains',
    'unless the cited lines are its whole definition',
    'Prefer "includes" and "such as"',
    "say they are examples",
    "Never write file, line, handler or percentage counts in prose",
    'Copy a number only from a digest field named count or stats, verbatim',
    "Samples are only some members",
    'such as "the removed call sat after return, so behaviour does not change"',
    'such as "the imports name something the target does not export"',
    "after reading the file around the cited lines",
    "If unsure, omit it",
)


def test_both_prompts_carry_the_only_claim_number_and_verified_fact_rules(tmp_path):
    for prompt in (llm.group_prompt(tmp_path, {"id": "api", "label": "API"}, tmp_path / "d.json"),
                   llm.merge_prompt(tmp_path, tmp_path / "m.json")):
        flat = " ".join(prompt.split())
        for phrase in NEW_RULE_PHRASES:
            assert phrase in flat, phrase
        assert "say how many files" not in flat
    group = " ".join(llm.group_prompt(tmp_path, {"id": "api", "label": "API"}, tmp_path / "d.json").split())
    assert "never for the directory of a sample member when its pattern.directories is above 1" in group


def test_pattern_digest_rows_carry_directories_and_common_dir_in_both_digests():
    snapshot, draft, group = pattern_world()
    members = ["zone0/a/x.ts", "zone1/a/x.ts", "zone2/b/y.ts", "zone3/z.ts", "zone3/w.ts"]
    draft["nodes"][-1]["pattern"].update(members=members, count=5)
    row = next(row for row in llm.build_group_digest(draft, group, snapshot)["rows"] if row["kind"] == "pattern")
    assert row["pattern"]["directories"] == 4 and row["pattern"]["common_dir"] == ""
    merged = next(row for row in llm.build_merge_digest(draft, {}, snapshot)["groups"][0]["rows"] if row["kind"] == "pattern")
    assert merged["pattern"]["directories"] == 4 and merged["pattern"]["common_dir"] == "" and merged["pattern"]["count"] == 5
    draft["nodes"][-1]["pattern"].update(members=["src/a/x.ts", "src/a/y.ts", "src/a/b/z.ts"], count=3)
    row = next(row for row in llm.build_group_digest(draft, group, snapshot)["rows"] if row["kind"] == "pattern")
    assert row["pattern"]["directories"] == 2 and row["pattern"]["common_dir"] == "src/a"


def test_unknown_pattern_families_pass_through_the_digests():
    snapshot, draft, group = pattern_world()
    draft["nodes"][-1]["pattern"]["family"] = "remainder"
    draft["nodes"][-1]["label"] = "20 other changed files"
    row = next(row for row in llm.build_group_digest(draft, group, snapshot)["rows"] if row["kind"] == "pattern")
    assert row["pattern"]["family"] == "remainder"
    merged = next(row for row in llm.build_merge_digest(draft, {}, snapshot)["groups"][0]["rows"] if row["kind"] == "pattern")
    assert merged["pattern"]["family"] == "remainder"
    draft["nodes"][-1]["pattern"] = {"family": 7, "count": 2}
    llm.build_group_digest(draft, group, snapshot)
    llm.build_merge_digest(draft, {}, snapshot)


def test_merge_digest_rows_carry_computed_stats():
    snapshot, draft, group = pattern_world()
    rows = {row["id"]: row for row in llm.build_merge_digest(draft, {}, snapshot)["groups"][0]["rows"]}
    assert rows["legacy-deleted"]["stats"]["files"] == 20 and rows["legacy-deleted"]["stats"]["deletions"] == 40


# --- orchestration -----------------------------------------------------------------------

def assert_valid(result, snapshot=None):
    snapshot = snapshot or world()[0]
    validate_v2(result["document"], HEAD, snapshot)
    assert result["document"]["head_sha"] == HEAD


def test_full_success_produces_an_llm_document_with_stats(tmp_path):
    snapshot, draft = world()
    stub = Stub(good_answers(snapshot))
    result = run(tmp_path, stub)
    assert_valid(result)
    document = result["document"]
    assert document["generated_by"] == "llm" and document["schema_version"] == 2
    assert rows_of(document)[ROW[REVIEWS]]["label"] == "api text" and rows_of(document)[ROW[NAMES]]["label"] == "lib text"
    assert document["title"] == "Reviews are validated" and len(document["steps"]) == 3
    assert result["stats"] == {"groups_total": 4, "groups_selected": 3, "groups_llm_ok": 3, "groups_fallback": 1,
                               "cache_hits": 0, "merge": "ok", "steps_dropped": 0, "llm_calls": 4, "degraded": None}
    assert len(stub.calls) == 4 and len(stub.who("merge")) == 1


def test_diagnostics_are_written_per_call(tmp_path):
    snapshot, draft = world()
    result = run(tmp_path, Stub(good_answers(snapshot)))
    folder = diag(result["dir"])
    names = {path.name for path in folder.iterdir()}
    assert {"group-api-input.json", "group-api-raw.txt", "group-db-input.json", "group-lib-input.json",
            "merge-input.json", "merge-raw.txt", "stats.json"} <= names
    assert json.loads((folder / "group-api-input.json").read_text())["group"]["id"] == "api"
    assert json.loads((folder / "merge-input.json").read_text())["groups"]
    assert json.loads((folder / "stats.json").read_text())["generated_by"] == "llm"
    assert not (folder / "group-api-errors.json").exists()


def test_prompts_reference_the_digest_files_that_exist(tmp_path):
    snapshot, _ = world()
    stub = Stub(good_answers(snapshot))
    result = run(tmp_path, stub)
    for call in stub.calls:
        name = "merge-input.json" if call["who"] == "merge" else f"group-{call['who']}-input.json"
        path = diag(result["dir"]) / name
        assert str(path) in call["prompt"] and path.is_file()
        assert len(call["prompt"].encode()) < 12 * 1024
        assert "const value" not in call["prompt"]


def test_stale_diagnostics_are_cleared(tmp_path):
    snapshot, _ = world()
    directory = artifact(tmp_path)
    folder = diag(directory)
    folder.mkdir(parents=True)
    (folder / "group-api-errors.json").write_text("stale")
    (folder / "keep.txt").write_text("other")
    run(tmp_path, Stub(good_answers(snapshot)), directory=directory)
    assert not (folder / "group-api-errors.json").exists() and (folder / "keep.txt").exists()


def test_first_invalid_output_is_retried_with_error_lines(tmp_path):
    snapshot, _ = world()
    bad_ref = dict(cite(snapshot, SESSION), start=500, end=501)
    first = json.dumps({"edges": [{"source": ROW[SESSION], "target": ROW[STORE], "label": "x", "claim": "y",
                                   "evidence": [bad_ref]}]})
    answers = good_answers(snapshot)
    answers["api"] = [first, simple_patch("api")]
    stub = Stub(answers)
    result = run(tmp_path, stub)
    api_calls = stub.who("api")
    assert len(api_calls) == 2 and api_calls[0]["prompt"] != api_calls[1]["prompt"]
    assert "edges[0].evidence[0].start: lines must lie inside one new-side hunk of the cited file" in api_calls[1]["prompt"]
    assert "Your previous answer was rejected" in api_calls[1]["prompt"]
    assert rows_of(result["document"])[ROW[REVIEWS]]["label"] == "api text"
    assert result["stats"]["groups_llm_ok"] == 3 and result["stats"]["llm_calls"] == 5
    assert (diag(result["dir"]) / "group-api-raw-1.txt").read_text() == first
    assert json.loads((diag(result["dir"]) / "group-api-errors-1.json").read_text())[0]["path"] == "edges[0].evidence[0].start"
    assert "retrying once" in result["log"]


def test_second_failure_keeps_the_deterministic_form_and_writes_diagnostics(tmp_path):
    snapshot, draft = world()
    answers = good_answers(snapshot)
    answers["api"] = ["not json", '{"rows": [{"id": "%s", "label": "%s"}]}' % (ROW[REVIEWS], "x" * 90)]
    stub = Stub(answers)
    result = run(tmp_path, stub)
    assert len(stub.who("api")) == 2
    assert rows_of(result["document"])[ROW[REVIEWS]]["label"] == "reviews.ts"
    assert result["stats"]["groups_llm_ok"] == 2 and result["stats"]["groups_fallback"] == 2
    folder = diag(result["dir"])
    assert (folder / "group-api-raw.txt").read_text().startswith('{"rows"')
    errors = json.loads((folder / "group-api-errors.json").read_text())
    assert errors[0]["path"] == "rows[0].label"
    assert "rejected twice" in result["log"]
    entries = [json.loads(path.read_text()) for path in cache_folder(tmp_path).glob("*.json")]
    assert sorted(entry["kind"] for entry in entries) == ["group", "group", "merge"], "api was not cached"


def test_null_answer_is_not_retried(tmp_path):
    snapshot, _ = world()
    answers = good_answers(snapshot)
    answers["api"] = "null"
    stub = Stub(answers)
    result = run(tmp_path, stub)
    assert len(stub.who("api")) == 1 and result["stats"]["groups_llm_ok"] == 2
    assert_valid(result)


def test_parallel_groups_are_really_in_flight_together(tmp_path):
    snapshot, _ = world()
    barrier = threading.Barrier(3, timeout=10)

    def wait_for_everyone(prompt, attempt):
        barrier.wait()
        return simple_patch(re.search(r'Group: id "([^"]+)"', prompt).group(1))

    answers = good_answers(snapshot)
    stub = Stub({**answers, **{group: wait_for_everyone for group in GROUP_ROWS}})
    result = run(tmp_path, stub, concurrency=3)
    assert stub.max_in_flight == 3 and result["stats"]["groups_llm_ok"] == 3


@pytest.mark.parametrize("bound", [1, 2, 3])
def test_concurrency_is_bounded(tmp_path, bound):
    snapshot, _ = world()
    stub = Stub(good_answers(snapshot), delay=0.15)
    result = run(tmp_path, stub, concurrency=bound)
    assert stub.max_in_flight == min(bound, 3) if bound < 3 else stub.max_in_flight > 1
    assert result["stats"]["groups_llm_ok"] == 3


def test_default_concurrency_runs_groups_in_parallel(tmp_path):
    snapshot, _ = world()
    stub = Stub(good_answers(snapshot), delay=0.15)
    run(tmp_path, stub)
    assert 1 < stub.max_in_flight <= llm.DEFAULT_CONCURRENCY


def test_environment_overrides_concurrency_groups_timeout_and_models(tmp_path, monkeypatch):
    snapshot, _ = world()
    monkeypatch.setenv("PAIRCLI_ARCH_CONCURRENCY", "1")
    monkeypatch.setenv("PAIRCLI_ARCH_LLM_GROUPS", "2")
    monkeypatch.setenv("PAIRCLI_ARCH_CALL_TIMEOUT", "30")
    monkeypatch.setenv("PAIRCLI_MODEL_GROUP", "group-model")
    monkeypatch.setenv("PAIRCLI_MODEL_MERGE", "merge-model")
    stub = Stub(good_answers(snapshot), delay=0.05)
    result = run(tmp_path, stub)
    assert stub.max_in_flight == 1 and [call["who"] for call in stub.group_calls].count("lib") == 0
    assert result["stats"]["groups_selected"] == 2
    assert {call["timeout"] for call in stub.calls} == {30}
    assert {call["model"] for call in stub.group_calls} == {"group-model"}
    assert stub.who("merge")[0]["model"] == "merge-model"


def test_defaults_and_no_hardcoded_models(tmp_path):
    snapshot, _ = world()
    stub = Stub(good_answers(snapshot))
    run(tmp_path, stub)
    assert {call["timeout"] for call in stub.calls} == {120}
    assert {call["model"] for call in stub.calls} == {None}


@pytest.mark.parametrize("name, value, default", [
    ("PAIRCLI_ARCH_LLM_GROUPS", "abc", 8), ("PAIRCLI_ARCH_LLM_GROUPS", "99", 12), ("PAIRCLI_ARCH_LLM_GROUPS", "-3", 0),
    ("PAIRCLI_ARCH_LLM_GROUPS", "", 8), ("PAIRCLI_ARCH_LLM_GROUPS", "2.5", 8),
])
def test_env_number_is_clamped_and_tolerant(monkeypatch, name, value, default):
    monkeypatch.setenv(name, value)
    assert llm.env_number(name, 8, 0, 12, int) == default


def test_env_number_floats(monkeypatch):
    monkeypatch.setenv("X", "nan")
    assert llm.env_number("X", 5.0, 1.0, 10.0) == 5.0
    monkeypatch.setenv("X", "0.1")
    assert llm.env_number("X", 5.0, 1.0, 10.0) == 1.0
    monkeypatch.setenv("X", "inf")
    assert llm.env_number("X", 5.0, 1.0, 10.0) == 5.0


def test_groups_limit_zero_makes_no_calls(tmp_path, monkeypatch):
    snapshot, draft = world()
    monkeypatch.setenv("PAIRCLI_ARCH_LLM_GROUPS", "0")
    stub = Stub(good_answers(snapshot))
    result = run(tmp_path, stub)
    assert stub.calls == [] and result["document"]["generated_by"] == "deterministic"
    assert result["stats"]["merge"] == "skipped" and result["stats"]["llm_calls"] == 0


def test_top_groups_are_chosen_by_skeleton_score_and_routine_only_groups_are_skipped(tmp_path):
    snapshot, _ = world()
    stub = Stub(good_answers(snapshot))
    result = run(tmp_path, stub, groups_limit=2, skeleton={"groups": [
        {"id": "lib", "score": 9.0}, {"id": "root", "score": 99.0}, {"id": "db", "score": 5.0}, {"id": "api", "score": 1.0}]})
    assert {call["who"] for call in stub.group_calls} == {"lib", "db"}
    assert result["stats"]["groups_selected"] == 2


def test_selection_without_a_skeleton_falls_back_to_change_size(tmp_path):
    snapshot, draft = world()
    selected = llm.select_groups(draft, snapshot, HEAD, None, 8)
    assert set(selected) == {"api", "db", "lib"} and "root" not in selected
    assert llm.select_groups(draft, snapshot, HEAD, None, 1) == selected[:1]


def test_call_timeout_is_min_of_call_timeout_and_remaining_budget(tmp_path):
    snapshot, _ = world()
    clock = FakeClock()

    def slow(prompt, attempt):
        who = re.search(r'Group: id "([^"]+)"', prompt)
        clock.advance(60)
        return simple_patch(who.group(1)) if who else json.dumps({})

    stub = Stub({"api": slow, "db": slow, "lib": slow, "merge": slow})
    result = run(tmp_path, stub, clock=clock, concurrency=1, deadline=100, call_timeout=120)
    timeouts = [call["timeout"] for call in stub.calls]
    assert timeouts[0] == 100 and timeouts[1] == 40, timeouts
    # api and db answered; lib had no time left, and the merge call was skipped
    assert [call["who"] for call in stub.calls] == ["api", "db"]
    assert result["stats"]["merge"] == "skipped" and result["stats"]["llm_calls"] == 2
    assert result["stats"]["groups_llm_ok"] == 2
    assert_valid(result)


def test_deadline_environment_variable_sets_the_budget(tmp_path, monkeypatch):
    snapshot, _ = world()
    clock = FakeClock()
    monkeypatch.setenv("PAIRCLI_ARCH_DEADLINE", "50")
    stub = Stub({group: (lambda prompt, attempt: clock.advance(30) or simple_patch(
        re.search(r'Group: id "([^"]+)"', prompt).group(1))) for group in GROUP_ROWS})
    result = run(tmp_path, stub, clock=clock, concurrency=1)
    assert [call["timeout"] for call in stub.calls] == [50, 20]
    assert result["stats"]["groups_llm_ok"] == 2 and result["stats"]["merge"] == "skipped"


def test_a_hanging_merge_call_is_abandoned_at_the_deadline(tmp_path):
    snapshot, _ = world()
    release = threading.Event()
    stub = Stub({**good_answers(snapshot), "merge": lambda prompt, attempt: release.wait(10) and "null"})
    started = time.monotonic()
    try:
        result = run(tmp_path, stub, deadline=0.6)
        assert time.monotonic() - started < 3
        assert_valid(result)
        assert result["stats"]["merge"] == "failed" and result["document"]["steps"] == []
        assert rows_of(result["document"])[ROW[REVIEWS]]["label"] == "api text"
        assert "stage deadline reached" in result["log"]
    finally:
        release.set()
    time.sleep(0.2)


def test_overall_deadline_abandons_stragglers_and_returns_promptly(tmp_path):
    snapshot, _ = world()
    release = threading.Event()

    def hang(prompt, attempt):
        release.wait(10)
        return simple_patch("db")

    stub = Stub({**good_answers(snapshot), "db": hang})
    started = time.monotonic()
    try:
        result = run(tmp_path, stub, deadline=0.5, call_timeout=60)
        elapsed = time.monotonic() - started
        assert elapsed < 3, f"returned after {elapsed:.1f}s"
        assert_valid(result)
        document = result["document"]
        assert rows_of(document)[ROW[STORE]]["label"] == "store.ts", "straggler keeps its deterministic form"
        assert rows_of(document)[ROW[REVIEWS]]["label"] == "api text"
        assert result["stats"]["groups_llm_ok"] == 2 and result["stats"]["merge"] == "skipped"
        assert "stage deadline reached" in result["log"]
    finally:
        release.set()
    time.sleep(0.3)  # the abandoned worker finishes quietly


def test_straggler_finishing_late_cannot_change_the_returned_document(tmp_path):
    snapshot, _ = world()
    release = threading.Event()
    stub = Stub({**good_answers(snapshot), "lib": lambda prompt, attempt: release.wait(5) and simple_patch("lib")})
    result = run(tmp_path, stub, deadline=0.4)
    before = json.dumps(result["document"])
    release.set()
    time.sleep(0.3)
    assert json.dumps(result["document"]) == before
    cache = tmp_path / ".paircli" / "cache" / "architecture-v2"
    assert not cache.exists() or all("lib" not in path.read_text() for path in cache.glob("*.json"))


def test_a_worker_that_raises_costs_only_its_group(tmp_path):
    snapshot, _ = world()
    stub = Stub({**good_answers(snapshot), "api": RuntimeError("boom")})
    result = run(tmp_path, stub)
    assert_valid(result)
    assert result["document"]["generated_by"] == "llm"
    assert rows_of(result["document"])[ROW[REVIEWS]]["label"] == "reviews.ts"
    assert rows_of(result["document"])[ROW[STORE]]["label"] == "db text"
    assert result["stats"]["groups_llm_ok"] == 2 and "RuntimeError" in result["log"]
    assert len(stub.who("api")) == 1, "crashes are not retried"


@pytest.mark.parametrize("error", [SystemExit(3), KeyboardInterrupt(), MemoryError(), OSError("disk"),
                                   subprocess.CalledProcessError(2, "claude")])
def test_even_odd_exceptions_from_the_runner_never_escape(tmp_path, error):
    snapshot, draft = world()
    stub = Stub({group: error for group in GROUP_ROWS} | {"merge": error})
    result = run(tmp_path, stub)
    assert_valid(result)
    assert result["document"]["generated_by"] == "deterministic"
    assert result["document"] == finalize_v2(draft, HEAD, snapshot, "deterministic")[0]


def test_subprocess_timeout_from_the_runner_keeps_the_group_and_is_not_retried(tmp_path):
    snapshot, _ = world()
    stub = Stub({**good_answers(snapshot), "db": subprocess.TimeoutExpired("claude", 5)})
    result = run(tmp_path, stub)
    assert_valid(result)
    assert len(stub.who("db")) == 1 and "timed out" in result["log"]
    assert rows_of(result["document"])[ROW[STORE]]["label"] == "store.ts"
    assert result["stats"]["groups_llm_ok"] == 2 and result["stats"]["merge"] == "ok"


@pytest.mark.parametrize("answer", [None, 42, b"bytes", ["a"], "", "   ", "{}", "null", "garbage <html>"])
def test_garbage_answers_fall_back_to_deterministic(tmp_path, answer):
    snapshot, draft = world()
    stub = Stub({}, default=answer)
    result = run(tmp_path, stub)
    assert_valid(result)
    assert result["document"] == finalize_v2(draft, HEAD, snapshot, "deterministic")[0]
    assert result["document"]["generated_by"] == "deterministic"
    assert result["stats"]["groups_llm_ok"] == 0 and result["stats"]["groups_fallback"] == 4


def test_all_groups_failing_skips_the_merge_call(tmp_path):
    snapshot, draft = world()
    stub = Stub({**good_answers(snapshot), **{group: RuntimeError("down") for group in GROUP_ROWS}})
    result = run(tmp_path, stub)
    assert stub.who("merge") == [] and result["stats"]["merge"] == "skipped"
    assert result["document"]["generated_by"] == "deterministic"


def test_partial_success_is_llm_and_full_failure_is_deterministic(tmp_path):
    snapshot, _ = world()
    partial = run(tmp_path / "a", Stub({"lib": simple_patch("lib")}), directory=artifact(tmp_path / "a"))
    assert partial["document"]["generated_by"] == "llm" and partial["stats"]["groups_llm_ok"] == 1
    none = run(tmp_path / "b", Stub({}), directory=artifact(tmp_path / "b"))
    assert none["document"]["generated_by"] == "deterministic"


def test_merge_failures_keep_the_graph(tmp_path):
    snapshot, _ = world()
    for label, answer in [("exception", RuntimeError("x")), ("timeout", subprocess.TimeoutExpired("c", 1)),
                          ("null", "null"), ("garbage", "nope"), ("array", "[]"), ("none", None)]:
        directory = artifact(tmp_path / label)
        stub = Stub({**good_answers(snapshot), "merge": answer})
        result = run(tmp_path / label, stub, directory=directory)
        assert_valid(result)
        document = result["document"]
        assert document["generated_by"] == "llm" and document["steps"] == [], label
        assert rows_of(document)[ROW[REVIEWS]]["label"] == "api text", label
        assert result["stats"]["merge"] == "failed", label


def test_merge_with_invalid_steps_keeps_valid_ones_and_counts_drops(tmp_path):
    snapshot, _ = world()
    answer = merge_answer(snapshot)
    answer["steps"].append({**answer["steps"][0], "id": "bad", "nodes": ["ghost"]})
    answer["steps"].append({"id": "after-removed", "when": "after", "nodes": [ROW[LEGACY]], "text": "x",
                            "evidence": [cite(snapshot, LEGACY)]})
    result = run(tmp_path, Stub({**good_answers(snapshot), "merge": answer}))
    assert_valid(result)
    assert [step["id"] for step in result["document"]["steps"]] == ["s1", "s2", "s3"]
    assert result["stats"]["steps_dropped"] == 2 and result["stats"]["merge"] == "ok"


def fail_combined(monkeypatch, fails):
    """Make the final `finalize_v2` reject documents for which `fails(document, generated_by)` is true,
    while the per-group check (which also calls finalize_v2) still sees the real one."""
    real = llm.finalize_v2
    original = llm.check_group_patch
    local = threading.local()

    def checking(*args, **kwargs):
        local.checking = True
        try:
            return original(*args, **kwargs)
        finally:
            local.checking = False

    def finalize(document, head, snap, generated_by):
        if not getattr(local, "checking", False) and fails(document, generated_by):
            raise ArchitectureError([{"path": "nodes[0].description", "reason": "rejected on purpose"}])
        return real(document, head, snap, generated_by)

    monkeypatch.setattr(llm, "check_group_patch", checking)
    monkeypatch.setattr(llm, "finalize_v2", finalize)


def test_combined_validation_failure_drops_llm_edges_first(tmp_path, monkeypatch):
    snapshot, draft = world()
    fail_combined(monkeypatch, lambda document, by: any(edge["label"] == "BOOM" for edge in document["edges"]))
    answers = good_answers(snapshot)
    answers["api"] = json.dumps({"rows": [{"id": ROW[REVIEWS], "label": "Reviews API"}], "edges": [
        {"source": ROW[SESSION], "target": ROW[STORE], "label": "BOOM", "claim": "x", "evidence": [cite(snapshot, SESSION)]}]})
    result = run(tmp_path, Stub(answers))
    assert_valid(result)
    document = result["document"]
    assert document["generated_by"] == "llm" and result["stats"]["degraded"] == "without LLM edges"
    assert rows_of(document)[ROW[REVIEWS]]["label"] == "Reviews API", "text edits survive"
    assert not any(edge["label"] == "BOOM" for edge in document["edges"])
    assert len(document["edges"]) == len(draft["edges"])
    assert [step["id"] for step in document["steps"]] == ["s1", "s2"], "steps that need dropped edges are dropped"
    assert "degraded" in result["log"]


def test_combined_validation_failure_reverts_only_the_culprit_group(tmp_path, monkeypatch):
    snapshot, draft = world()
    fail_combined(monkeypatch, lambda document, by: any("POISON" in str(node["description"]) for node in document["nodes"]))
    answers = good_answers(snapshot, merge=False)
    answers["db"] = json.dumps({"rows": [{"id": ROW[STORE], "description": "POISON"}]})
    result = run(tmp_path, Stub(answers), concurrency=1)
    assert_valid(result)
    document = result["document"]
    assert rows_of(document)[ROW[STORE]]["description"] == "Changes src/db/store.ts."
    assert rows_of(document)[ROW[REVIEWS]]["label"] == "api text" and rows_of(document)[ROW[NAMES]]["label"] == "lib text"
    assert result["stats"]["degraded"] == "without groups db" and result["stats"]["groups_llm_ok"] == 2
    assert result["stats"]["groups_fallback"] == 2 and document["generated_by"] == "llm"
    assert "degraded" in result["log"]


def test_combined_validation_failure_with_a_culprit_in_every_pair_drops_lowest_ranked_first(tmp_path, monkeypatch):
    snapshot, draft = world()
    # Fails only when api and db are both narrated: no single removal of lib helps, but dropping db or api does.
    fail_combined(monkeypatch, lambda document, by: rows_of(document)[ROW[REVIEWS]]["label"] == "api text"
                  and rows_of(document)[ROW[STORE]]["label"] == "db text")
    result = run(tmp_path, Stub(good_answers(snapshot, merge=False)), concurrency=1)
    assert_valid(result)
    document = result["document"]
    assert rows_of(document)[ROW[STORE]]["label"] == "store.ts", "the lowest-ranked of the two is reverted"
    assert rows_of(document)[ROW[REVIEWS]]["label"] == "api text" and rows_of(document)[ROW[NAMES]]["label"] == "lib text"
    assert result["stats"]["degraded"] == "without groups db"


def test_combined_validation_failure_can_end_at_the_deterministic_document(tmp_path, monkeypatch):
    snapshot, draft = world()
    fail_combined(monkeypatch, lambda document, by: by == "llm")
    result = run(tmp_path, Stub(good_answers(snapshot)), concurrency=1)
    assert result["document"] == finalize_v2(draft, HEAD, snapshot, "deterministic")[0]
    assert result["document"]["generated_by"] == "deterministic"
    assert result["stats"]["degraded"].startswith("without groups") and result["stats"]["groups_llm_ok"] == 0
    assert result["stats"]["groups_fallback"] == 4


def test_combined_validation_failure_with_merge_steps_retries_without_the_merge(tmp_path, monkeypatch):
    snapshot, draft = world()
    fail_combined(monkeypatch, lambda document, by: bool(document["steps"]))
    result = run(tmp_path, Stub(good_answers(snapshot)))
    assert_valid(result)
    assert result["stats"]["degraded"] == "without merge" and result["document"]["steps"] == []
    assert rows_of(result["document"])[ROW[REVIEWS]]["label"] == "api text" and result["stats"]["groups_llm_ok"] == 3


def test_output_is_deterministic_given_deterministic_stubs(tmp_path):
    snapshot, _ = world()
    outputs = []
    for name, concurrency in (("a", 1), ("b", 3), ("c", 3)):
        directory = artifact(tmp_path / name)
        result = run(tmp_path / name, Stub(good_answers(snapshot), delay=0.02), directory=directory, concurrency=concurrency)
        outputs.append((json.dumps(result["document"]), result["warnings"], result["stats"]))
    assert outputs[0] == outputs[1] == outputs[2]


def test_the_input_draft_is_never_mutated(tmp_path):
    snapshot, draft = world()
    before = copy.deepcopy(draft)
    run(tmp_path, Stub(good_answers(snapshot)), snapshot=snapshot, draft=draft)
    assert draft == before


def test_invalid_arguments_raise_but_model_problems_do_not(tmp_path):
    snapshot, draft = world()
    directory = artifact(tmp_path)
    with pytest.raises(TypeError):
        llm.generate_v2(str(directory), snapshot, HEAD, draft, Stub(), io.StringIO())
    with pytest.raises(TypeError):
        llm.generate_v2(directory, snapshot, HEAD, draft, "not callable", io.StringIO())
    with pytest.raises(TypeError):
        llm.generate_v2(directory, snapshot, HEAD, None, Stub(), io.StringIO())
    with pytest.raises(TypeError):
        llm.generate_v2(directory, snapshot, HEAD, draft, Stub(), object())
    with pytest.raises(ValueError):
        llm.generate_v2(directory, snapshot, HEAD, draft, Stub(), io.StringIO(), concurrency=0)


def test_an_invalid_deterministic_draft_is_reported_not_narrated(tmp_path):
    snapshot, draft = world()
    draft["nodes"][0]["evidence"] = []
    stub = Stub(good_answers(snapshot))
    with pytest.raises(ArchitectureError):
        run(tmp_path, stub, snapshot=snapshot, draft=draft)
    assert stub.calls == []


def test_a_broken_log_cannot_break_generation(tmp_path):
    snapshot, draft = world()

    class BrokenLog:
        def write(self, message):
            raise OSError("closed")

    result = llm.generate_v2(artifact(tmp_path), snapshot, HEAD, draft, Stub(good_answers(snapshot)), BrokenLog(),
                             skeleton=SCORES, min_call_seconds=0)
    assert result["document"]["generated_by"] == "llm"


def test_unwritable_diagnostics_fall_back_to_the_deterministic_document(tmp_path):
    snapshot, draft = world()
    directory = artifact(tmp_path)
    (directory / "diagnostics").write_text("a file, not a folder")
    stub = Stub(good_answers(snapshot))
    result = run(tmp_path, stub, directory=directory)
    assert_valid(result)
    assert stub.calls == [] and result["document"]["generated_by"] == "deterministic"
    assert "diagnostics not written" in result["log"]


def test_symlinked_diagnostics_are_refused(tmp_path):
    snapshot, draft = world()
    directory = artifact(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (directory / "diagnostics").symlink_to(elsewhere)
    result = run(tmp_path, Stub(good_answers(snapshot)), directory=directory)
    assert_valid(result)
    assert list(elsewhere.iterdir()) == []


# --- cache -------------------------------------------------------------------------------

def cache_folder(tmp_path) -> Path:
    return tmp_path / ".paircli" / "cache" / "architecture-v2"


def test_cache_miss_then_hit_costs_no_llm_call(tmp_path):
    snapshot, _ = world()
    first = run(tmp_path, Stub(good_answers(snapshot)))
    assert first["stats"]["cache_hits"] == 0 and len(list(cache_folder(tmp_path).glob("*.json"))) == 4
    stub = Stub(good_answers(snapshot))
    second = run(tmp_path, stub, directory=first["dir"])
    assert stub.calls == [] and second["stats"]["llm_calls"] == 0
    assert second["stats"]["cache_hits"] == 4 and second["stats"]["groups_llm_ok"] == 3 and second["stats"]["merge"] == "ok"
    assert second["document"] == first["document"]
    assert second["log"].count(": cache hit") == 4


def test_cache_is_outside_the_pr_folder(tmp_path):
    snapshot, _ = world()
    result = run(tmp_path, Stub(good_answers(snapshot)))
    assert cache_folder(tmp_path).is_dir()
    assert not any("cache" in path.parts for path in result["dir"].rglob("*"))


def test_only_validated_patches_are_cached(tmp_path):
    snapshot, _ = world()
    run(tmp_path, Stub({**good_answers(snapshot), "api": "garbage"}))
    entries = [json.loads(path.read_text()) for path in cache_folder(tmp_path).glob("*.json")]
    assert len(entries) == 3 and all(entry["version"] == llm.PROMPT_VERSION for entry in entries)
    assert all("garbage" not in json.dumps(entry) for entry in entries)
    assert {entry["kind"] for entry in entries} == {"group", "merge"}


def test_failed_groups_leave_no_cache_entry(tmp_path):
    snapshot, _ = world()
    run(tmp_path, Stub({"api": RuntimeError("x"), "db": "null", "lib": ["bad", "worse"]}))
    folder = cache_folder(tmp_path)
    assert not folder.exists() or list(folder.glob("*.json")) == []


@pytest.mark.parametrize("damage", [
    lambda path: path.write_text("{not json"),
    lambda path: path.write_bytes(b"\xff\xfe\x00"),
    lambda path: path.write_text("[1]"),
    lambda path: path.write_text(json.dumps({"version": "old", "kind": "group", "patch": {}})),
    lambda path: path.write_text(json.dumps({"version": llm.PROMPT_VERSION, "kind": "group", "patch": "x"})),
    lambda path: path.write_text(" " * (llm.CACHE_ENTRY_MAX_BYTES + 10)),
    lambda path: (path.unlink(), path.mkdir()),
])
def test_unreadable_corrupt_or_oversized_entries_are_misses(tmp_path, damage):
    snapshot, _ = world()
    first = run(tmp_path, Stub(good_answers(snapshot)))
    for path in cache_folder(tmp_path).glob("*.json"):
        damage(path)
    stub = Stub(good_answers(snapshot))
    second = run(tmp_path, stub, directory=first["dir"])
    assert len(stub.calls) == 4 and second["stats"]["cache_hits"] == 0
    assert second["document"] == first["document"]


def test_a_cached_patch_that_no_longer_fits_the_draft_is_a_miss(tmp_path):
    snapshot, draft = world()
    key = llm.group_cache_key(draft, "api", snapshot, "codex", None)
    llm.PatchCache.for_artifact(artifact(tmp_path)).put(key, "group", {
        "rows": [], "edges": [{"source": "ghost", "target": ROW[STORE], "label": "x", "claim": "y",
                               "evidence": [cite(snapshot, SESSION)]}], "summary": ""})
    stub = Stub(good_answers(snapshot))
    result = run(tmp_path, stub, directory=tmp_path / ".paircli" / "pr-42" / HEAD)
    assert len(stub.who("api")) == 1 and result["stats"]["cache_hits"] == 0


def test_cache_is_pruned_to_the_newest_entries(tmp_path):
    cache = llm.PatchCache.for_artifact(artifact(tmp_path))
    keys = [f"{index:064x}" for index in range(llm.CACHE_MAX_ENTRIES + 15)]
    for index, key in enumerate(keys):
        assert cache.put(key, "group", {"rows": [], "edges": [], "summary": str(index)})
        os.utime(cache_folder(tmp_path) / f"{key}.json", (1_000_000 + index, 1_000_000 + index))
    assert cache.put("f" * 64, "group", {"rows": [], "edges": [], "summary": "newest"})
    names = {path.stem for path in cache_folder(tmp_path).glob("*.json")}
    assert len(names) == llm.CACHE_MAX_ENTRIES
    assert "f" * 64 in names and keys[-1] in names and keys[0] not in names and keys[14] not in names


def test_oversized_patches_are_not_cached(tmp_path):
    cache = llm.PatchCache.for_artifact(artifact(tmp_path))
    assert cache.put("a" * 64, "group", {"summary": "x" * (llm.CACHE_ENTRY_MAX_BYTES + 1)}) is False
    assert not cache_folder(tmp_path).exists()


def test_symlinked_cache_folders_are_refused(tmp_path):
    snapshot, _ = world()
    directory = artifact(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / ".paircli" / "cache").symlink_to(target)
    stub = Stub(good_answers(snapshot))
    result = run(tmp_path, stub, directory=directory)
    assert_valid(result)
    assert list(target.rglob("*")) == []
    assert llm.PatchCache.for_artifact(directory).put("b" * 64, "group", {"rows": []}) is False
    assert llm.PatchCache.for_artifact(directory).get("b" * 64, "group") is None
    stub2 = Stub(good_answers(snapshot))
    run(tmp_path, stub2, directory=directory)
    assert len(stub2.calls) == 4, "nothing was cached, so every call is made again"


def test_symlinked_cache_entries_are_misses(tmp_path):
    snapshot, draft = world()
    directory = artifact(tmp_path)
    cache = llm.PatchCache.for_artifact(directory)
    key = "c" * 64
    assert cache.put(key, "group", {"rows": [], "edges": [], "summary": "ok"})
    real = cache_folder(tmp_path) / f"{key}.json"
    moved = tmp_path / "moved.json"
    real.rename(moved)
    real.symlink_to(moved)
    assert cache.get(key, "group") is None
    assert cache.put(key, "group", {"rows": []}) is False


def test_cache_is_disabled_without_a_repo_root_or_when_asked(tmp_path):
    assert llm.PatchCache.for_artifact(Path("/a/b")).folder is None
    assert llm.PatchCache.for_artifact(Path("/a/b")).get("a" * 64, "group") is None
    assert llm.PatchCache.for_artifact(Path("/a/b")).put("a" * 64, "group", {}) is False
    snapshot, _ = world()
    run(tmp_path, Stub(good_answers(snapshot)), use_cache=False)
    assert not cache_folder(tmp_path).exists()


def test_cache_entry_of_another_kind_is_a_miss(tmp_path):
    cache = llm.PatchCache.for_artifact(artifact(tmp_path))
    assert cache.put("d" * 64, "merge", {"edges": []})
    assert cache.get("d" * 64, "group") is None and cache.get("d" * 64, "merge") == {"edges": []}


def test_cache_rejects_keys_that_are_not_hashes(tmp_path):
    cache = llm.PatchCache.for_artifact(artifact(tmp_path))
    assert cache.put("../../escape", "group", {}) is False
    assert cache.get("../../escape", "group") is None


def test_cache_key_changes_with_a_files_hunks_but_not_with_other_groups():
    snapshot, draft = world()
    base = llm.group_cache_key(draft, "api", snapshot, "codex", None)
    assert base == llm.group_cache_key(draft, "api", copy.deepcopy(snapshot), "codex", None)

    changed = copy.deepcopy(snapshot)
    next(file for file in changed["pr"]["files"] if file["path"] == REVIEWS)["hunks"][0]["lines"][1]["text"] = "edited"
    assert llm.group_cache_key(draft, "api", changed, "codex", None) != base
    assert llm.group_cache_key(draft, "db", changed, "codex", None) == llm.group_cache_key(draft, "db", snapshot, "codex", None)
    assert llm.group_cache_key(draft, "lib", changed, "codex", None) == llm.group_cache_key(draft, "lib", snapshot, "codex", None)


def test_cache_key_covers_harness_model_prompt_version_edges_and_snapshot_hash(monkeypatch):
    snapshot, draft = world()
    base = llm.group_cache_key(draft, "api", snapshot, "codex", None)
    assert llm.group_cache_key(draft, "api", snapshot, "claude-code", None) != base
    assert llm.group_cache_key(draft, "api", snapshot, "codex", "some-model") != base
    assert llm.group_cache_key(draft, "db", snapshot, "codex", None) != base
    hashed = dict(snapshot, hash="abc")
    assert llm.group_cache_key(draft, "api", hashed, "codex", None) != base
    other = copy.deepcopy(draft)
    other["edges"][0]["evidence"][0]["start"] += 1
    assert llm.group_cache_key(other, "api", snapshot, "codex", None) != base
    monkeypatch.setattr(llm, "PROMPT_VERSION", "other-version")
    assert llm.group_cache_key(draft, "api", snapshot, "codex", None) != base


def test_changed_hunks_invalidate_only_the_affected_group_in_a_rerun(tmp_path):
    snapshot, draft = world()
    first = run(tmp_path, Stub(good_answers(snapshot)), snapshot=snapshot, draft=draft)
    changed = copy.deepcopy(snapshot)
    next(file for file in changed["pr"]["files"] if file["path"] == NAMES)["hunks"][0]["lines"][1]["text"] = "edited"
    stub = Stub(good_answers(snapshot))
    second = run(tmp_path, stub, snapshot=changed, draft=draft, directory=first["dir"])
    assert {call["who"] for call in stub.calls} == {"lib", "merge"}
    assert second["stats"]["cache_hits"] == 2


def test_merge_cache_key_depends_on_the_group_patches(tmp_path):
    snapshot, draft = world()
    digest = llm.build_merge_digest(draft, {}, snapshot)
    base = llm.merge_cache_key(digest, [("api", {"rows": []})], snapshot, "codex", None)
    assert llm.merge_cache_key(digest, [("api", {"rows": [1]})], snapshot, "codex", None) != base
    assert llm.merge_cache_key(digest, [], snapshot, "codex", None) != base
    assert llm.merge_cache_key(digest, [("api", {"rows": []})], snapshot, "claude-code", None) != base


def test_cache_hit_with_a_failing_other_group_still_calls_only_that_group(tmp_path):
    snapshot, _ = world()
    first = run(tmp_path, Stub({"api": simple_patch("api")}))
    stub = Stub({"api": RuntimeError("never called"), "db": simple_patch("db")})
    second = run(tmp_path, stub, directory=first["dir"])
    assert stub.who("api") == [] and second["stats"]["cache_hits"] == 1
    assert rows_of(second["document"])[ROW[REVIEWS]]["label"] == "api text"


# --- scale -------------------------------------------------------------------------------

def test_large_snapshot_prompts_stay_small_and_the_document_is_valid(tmp_path):
    snapshot = make_snapshot(many_files(253, directories=[f"services/svc{index}/src" for index in range(10)]))
    draft = stub_draft(snapshot, depth=2, per_group=6)
    names = [group["id"] for group in draft["groups"]]
    answers = {name: json.dumps({"rows": [{"id": next(node["id"] for node in draft["nodes"] if node["group"] == name),
                                          "label": f"{name} label"}]}) for name in names}
    stub = Stub(answers, default="null")
    result = run(tmp_path, stub, snapshot=snapshot, draft=draft, skeleton=None)
    validate_v2(result["document"], HEAD, snapshot)
    assert result["stats"]["groups_selected"] == 8
    assert all(len(call["prompt"].encode()) < 12 * 1024 for call in stub.calls)
    assert all("const value" not in call["prompt"] for call in stub.calls)


# --- patches on pattern rows (a real deterministic draft) -----------------------------------

def real_pattern_world():
    from paircli_architecture_v2_draft import deterministic_v2_draft
    from paircli_skeleton import build_skeleton
    from snapshot_factory import deletion_heavy_pr
    snapshot = make_snapshot(deletion_heavy_pr(templates=12, features=5))
    draft = deterministic_v2_draft(build_skeleton(snapshot, HEAD), snapshot, HEAD)
    row = next(node for node in draft["nodes"] if node["kind"] == "pattern" and node["pattern"]["count"] == 12)
    return snapshot, draft, row


def test_a_patch_edits_the_text_of_a_pattern_row_but_never_its_structure():
    snapshot, draft, row = real_pattern_world()
    before = copy.deepcopy(row)
    patch = {"rows": [{"id": row["id"], "label": "Legacy templates removed",
                       "description": "The template engine no longer ships its built-in templates.",
                       "kind": "service", "pattern": {"family": "docs", "count": 3, "members": []},
                       "files": ["src/legacy/index.ts"], "evidence": [], "stats": {"files": 1}}]}
    combined, warnings = llm.apply_group_patch(draft, row["group"], patch, HEAD, snapshot)
    edited = next(node for node in combined["nodes"] if node["id"] == row["id"])
    assert edited["label"] == "Legacy templates removed" and edited["description"].startswith("The template engine")
    for key in ("kind", "pattern", "files", "evidence"):
        assert edited[key] == before[key]
    assert "stats" not in edited
    assert any("kind: pattern rows keep kind pattern" in warning for warning in warnings)
    for key in ("pattern", "files", "evidence", "stats"):
        assert any(f".{key}: not editable; ignored" in warning for warning in warnings)
    document, finalize_warnings = finalize_v2(combined, HEAD, snapshot, "llm")
    assert finalize_warnings == [] and validate_v2(document, HEAD, snapshot) is None
    shown = next(node for node in document["nodes"] if node["id"] == row["id"])
    assert shown["stats"]["files"] == 12 and shown["label"] == "Legacy templates removed"


def test_no_row_can_become_a_pattern_row_through_a_patch():
    snapshot, draft, row = real_pattern_world()
    file_row = next(node for node in draft["nodes"] if node["kind"] == "file")
    combined, warnings = llm.apply_group_patch(
        draft, file_row["group"], {"rows": [{"id": file_row["id"], "kind": "pattern", "label": "Renamed"}]},
        HEAD, snapshot)
    changed = next(node for node in combined["nodes"] if node["id"] == file_row["id"])
    assert changed["kind"] == "file" and changed["label"] == "Renamed" and "pattern" not in changed
    assert any("others cannot become pattern" in warning for warning in warnings)


def test_group_cache_keys_change_when_pattern_membership_signature_or_family_changes():
    snapshot, draft, row = real_pattern_world()
    key = lambda doc: llm.group_cache_key(doc, row["group"], snapshot, "harness", "model")  # noqa: E731
    assert key(draft) == key(copy.deepcopy(draft))
    smaller = copy.deepcopy(draft)
    target = next(node for node in smaller["nodes"] if node["id"] == row["id"])
    target["pattern"]["members"].pop()
    target["pattern"]["count"] -= 1
    assert key(smaller) != key(draft), "a member left the pattern"
    for field, value in (("signature", "f" * 16), ("family", "docs")):
        other = copy.deepcopy(draft)
        next(node for node in other["nodes"] if node["id"] == row["id"])["pattern"][field] = value
        assert key(other) != key(draft), field
    swapped = copy.deepcopy(draft)
    members = next(node for node in swapped["nodes"] if node["id"] == row["id"])["pattern"]["members"]
    members[-1] = "src/legacy/templates/elsewhere.ts"
    assert key(swapped) != key(draft), "same count, different member"
