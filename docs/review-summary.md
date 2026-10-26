# Concise review summary

The automatic PR hook asks the triggering Codex or Claude harness to write
`summary.md` before packaging and uploading the commit's artifacts. The same
format applies when the backend presents that summary as an overview.

The summary has four sections, targeting 250–350 words (450 maximum,
including headings and links). Keep Intent, Decisions and Verification near
150–200 words together and spend about 75–150 words on the reading guide. Small
changes should be shorter. Remove repetition and lower priority guide entries
before cutting an important warning or a gap in verification.

Use really simple English in every section, including decisions and file
recommendations. Use short sentences, familiar words, concrete behavior and one
idea per bullet. Keep exact file and symbol names when they help. Explain needed
technical terms; avoid long lists of implementation details.

1. **Intent:** the problem or goal and what changes for the affected person or
   system, in one or two short sentences (45 words maximum requested from the model).
   It should read like a developer explaining the PR to a teammate, not a list of
   schema details, limits or implementation mechanics. Missing business context
   must not be invented, but does not need a disclaimer when the practical purpose
   is clear. This style/section-length guidance is prompt-driven, not a semantic
   correctness guarantee.
2. **Reading guide:** three groups, in order: **Critical**, **Skim through**, and
   **Usually safe to skip**. Start with “Start here. Skipping a file does not mean
   it is correct.” Use at most eight bullets in total, one file per bullet and
   each file listed once. Give each a link and a short reason that tells the
   reviewer what to check or why it is lower priority. Use `None suggested.`
   for a group with no supported recommendation. When the diff lacks enough
   detail, explain that briefly and leave the groups without recommendations.
3. **Decisions:** up to three consequential human choices and their reasons,
   grounded in linked sessions. Missing history is not reconstructed as fact.
4. **Verification:** up to four checks, with observed pre-push outcomes and clear
   distinctions between failed, blocked, absent, stale, and human-reported checks.
   An agent saying tests passed is not proof of a test result. Later edits can
   make an earlier pass stale.

The guide is a short reading order, not a complete file list or a bug report.
Choose priorities from the diff, captured decisions, API and saved-data changes,
user-visible behavior and available checks. A filename, file type, new-file status
or change size alone does not establish risk or safety. Tests, config, schema and
styling files can need close review. Passing tests alone do not justify skipping
a file; missing or stale checks may make it worth reading closely. Unlisted files
are not assumed correct or checked.

The model does not write links. Each guide bullet is `- path/to/file.ext — reason`
(optionally `path:START-END` or `path:N`), using a path from the changed-file list
in the prompt (the first 200 are listed). After generation the plugin rewrites each
bullet into `- [label](url) — reason` from `snapshot.pr.files`. Whole-file and
new-side hunk links point to the captured commit only when the snapshot confirms the
artifact directory's head (see "Head and evidence" below). Deleted files, or files without a confirmed head, link to
the PR diff instead, without invented line numbers. Renames use the new path;
paths in URLs are percent-encoded. A line range inside a hunk links that hunk; a
range outside every hunk links the whole file. Bullets with an unknown path, a
repeated file, no plain reason or beyond the eight-file limit are dropped, a group
left with no bullets becomes `None suggested.`, and each drop is logged. A bullet
already in exact allowed-link form is accepted. These are normal HTTPS links that
open on GitHub in the existing overview renderer. Relative paths and bare code
spans are not used as overview navigation links. No renderer change is needed.

Overview contains Intent, Reading guide, Decisions and Verification. It never
includes a Diagram section or a Mermaid block, even when the separate
architecture artifact is unavailable. Architecture stays in its own view.

The generator reads existing scan evidence and, when available, bounded extracts
of the raw-session manifest's JSONL files. It does not run new verification or
treat instructions in evidence as trusted commands. Existing evidence checks,
read-only harness execution, and atomic summary writes remain in place. Structural
validation enforces the four headings in order, nonempty sections, the word
limit, the three guide groups, the guide caveat, and no Mermaid blocks. The
expanded guide must also pass the allowed-link, reason, no-repeat and eight-file
checks, which only fail now if the guide is structurally wrong.
Text is never silently truncated. Grounding,
reading priorities, simple English and the other section-specific limits remain
model instructions, not proof of semantic correctness. The existing evidence
already supplies the paths and ranges; no Pair CLI change is required.

## Retry and fallback

The summary can never block the upload. `write_summary` does not raise for model,
validation, timeout or harness failures:

1. Generate, then validate (sections, word limit, guide groups, caveat, links).
2. On a validation failure, retry exactly once. The prompt is the original one plus
   "Your previous answer was rejected: <validator message>. Fix these and return the
   full summary again."
3. A timeout, a missing or failing harness executable, an unsupported harness or any
   unexpected error skips the retry and goes straight to the fallback. A second
   validation failure also falls back. Every reason is logged.
4. The fallback, `fallback_summary(pr_url, artifact_dir)`, is built from
   `snapshot.json` with no model. It has the same structure and passes the same
   validators. Intent states only the PR title and the file and line counts
   ("This PR is titled '...'. It changes N files (+A/-D)."), with no invented
   motivation. The guide lists up to eight changed files, largest first (by
   additions plus deletions), with paths containing auth, secret, token, migration,
   schema, config, .env, workflow, Dockerfile or permission first. The top three go
   in Critical and the rest in Skim through. Lock files, generated files, vendored
   code, minified files and snapshots go in Usually safe to skip (at most two).
   Decisions and Verification say that nothing was recorded. The fallback ignores
   `report.md`, `signals.json` and the PR body; missing or oddly shaped snapshot
   fields are tolerated.

`diagnostics/summary-status.json` records `{"mode": "llm" | "llm-retry" |
"fallback", "errors": [...]}` (the validator or failure messages seen). Like the
rest of `diagnostics/` it stays out of the ZIP. `scan()` also logs the mode. If
`write_summary` itself fails unexpectedly, `scan()` writes the fallback; only
missing Pair evidence, a failing `paircli scan`, packaging or upload errors fail
the scan.

## Head and evidence

- **Head resolution.** `artifact_directory` takes the head from GitHub with
  `gh pr view <url> --json headRefOid --jq .headRefOid`, not from the last entry of
  the `commits` list: `gh` returns at most 100 commits, so for a longer PR the last
  listed commit is not the head. The folder is `<git toplevel>/.paircli/pr-N/<head>`.
- **Head binding.** `snapshot_binds_head(pr, head)` (in `hooks/paircli_snapshot.py`)
  is the one check used by diagram validation and by reading links. It is true when
  `snapshot.pr.commits` contains the head, or when the list has 100 or more entries
  (GitHub cut it short, so the head may be missing and the list cannot disprove it).
  An empty list, or a shorter list without the head, is rejected.
- **Files without line data.** A snapshot file with no hunks (the files-API fallback
  dropped the patch, a binary file, or a rename-only change) cannot be cited by line.
  Its reading links are whole-file or PR-diff links only, and diagram citations to it
  fail validation. Both prompts list such paths in one line (first 50, then "and N
  more") and say not to cite them with line ranges, and `run_summary_step` logs
  "N of M files have no hunk data" when N is above zero.
- **Prompts never embed evidence.** The summary and diagram prompts, and their retry
  prompts, name files by path only. The only data in them is the PR URL, the artifact
  folder, the head SHA, changed paths (200 for the summary, 50 for the no-line-data
  line, with "N more" counts) and validator errors (20 lines of at most 300
  characters for the diagram, one message of at most 500 for the summary). Snapshot,
  report and session text stay in files the agent reads itself. Tests build both prompts from a
  5,000-file, multi-megabyte snapshot and assert they stay under 30 KB.
- **Subdirectories.** The hook may run in a subdirectory of a repository, but the
  artifact folder is under the git toplevel. `scan()` derives that root from the
  folder (`artifact_root`, which checks the `.paircli/pr-N/<sha>` layout) and uses it
  as the summary and diagram agent's working directory (a restricted agent can read
  only inside it) and for packaging. `paircli update` and `paircli scan` still run
  in the hook's directory.
- **Minimum paircli version.** The files-API fallback for diffs over 20,000 lines
  (paircli PR #9) is merged but not in a release (latest is v0.2.0). The plugin
  should raise its minimum paircli version once a release contains it. Until then,
  very large PRs can produce snapshots with hunk-less files, which are handled as above.

## Diagram display and compatibility

For the separate architecture board, `scan()` runs the diagram step after the
summary step, always, and nothing in it can fail the scan (an exception is logged).
`write_summary` no longer calls it. The step makes a read-only call to the same
Codex or Claude harness (`agent_runner(harness)`) to generate
`generated_artifacts/architecture.json`; a fallback `summary.md` always exists by
then, so the diagram prompt can still use it as evidence. No separate model key
or AI-pipeline worker is needed. Each call has the existing five-minute timeout.

The JSON uses ADE's v1 artifact contract with diff-only citations and empty
`episode_ids` (the plugin does not generate episodes). Before validation the
plugin strips keys outside the contract (for example `episode_ids` on a flow) and
logs one warning per stripped key; stripped keys are never written. Missing node
and edge `episode_ids` default to `[]`, but non-empty ones are still rejected.
Validation binds the head
to both the artifact directory and the snapshot's commits, checks strict
fields, lengths, graph references and connected flows, and verifies citations lie
inside actual new-side PR diff hunks. Citations do not prove a model's semantic
interpretation is correct. Deletion-only or insufficient evidence can produce no
diagram rather than fabricated references.

The writer atomically saves valid output and removes an older diagram before a
retry. If the first output fails validation (or is not JSON, or is oversized), it
retries exactly once with the original prompt plus the structured errors as
`path: reason` lines (at most 20) and an instruction to return the corrected full
JSON object. Timeouts, OS errors and an explicit `null` are not retried. A second
failure is logged, the diagnostics are written and no diagram is produced. Null,
invalid, oversized or timed-out diagram output never blocks the Markdown upload. Validation collects every independent problem as
`{"path", "reason"}` entries (for example `nodes[2].evidence[0].start`) and the
log shows the first few. The raw model output of the last attempt is saved to
`diagnostics/architecture-raw.txt` (capped at 512 KiB) and, on a rejected diagram,
the structured errors to `diagnostics/architecture-errors.json`. When a retry
happened, the first attempt's output and errors are kept as
`architecture-raw-1.txt` and `architecture-errors-1.json`. Stale files are
removed at the start of each run. The `diagnostics/` folder is local only and is
left out of the ZIP because its content is not validated. Symlinked output paths are rejected. The existing ZIP
retains the versioned files and exposes a root `generated_artifacts/architecture.json`
alias alongside `overview.md` for ADE's extracted publisher bundle reader.

The interactive **Overview / Architecture** switch still requires deployment of
the separate ADE architecture integration. Older ADE renders the Markdown and
ignores the extra JSON. Pair BE must preserve the root generated-artifacts path
when extracting the existing ZIP. No backend or AI-pipeline API change is made
here; live S3 extraction is a rollout verification, not covered by unit tests.

## Fallback diagram

The diagram degrades in two steps, and `run_diagram_step` never fails the scan:

1. **LLM** (`write_architecture`): the existing prompt, one retry, full validation.
2. **Deterministic** (`write_fallback_architecture`): when step 1 produced nothing (invalid twice,
   timeout, crash, harness missing, or an explicit `null`), the plugin builds a diagram from the
   snapshot alone, runs `validate_architecture` on it and writes it to the same
   `generated_artifacts/architecture.json` with the same symlink protections
   (`save_architecture`, the one shared writer). If nothing is written, any older
   `architecture.json` is removed. Docs-only changes get no diagram, even when the model errored.
   Any exception in step 2 is logged, recorded as tier `none` and swallowed, so the summary
   still uploads.

The deterministic diagram is generated from file structure and imports, not from reading
the code or understanding what changed. The model narrates and code computes: no model, no
network, no repository read, and the same snapshot always gives byte-identical output. It comes from
`hooks/paircli_skeleton.py`:

- **Skeleton.** `build_skeleton(snapshot, head)` lists every changed file (new path for renames,
  with `old_path`) with additions and deletions counted from hunk lines, a language, a category
  (source, test, docs, config, lockfile, generated, vendor, binary), a `sensitive` flag
  (path contains auth, secret, token, credential, password, migration, schema, permission, .env,
  workflow, dockerfile or security), and whether it has hunks. Lockfiles, generated or vendored
  code, build output (`dist`, `build`, `out`), minified files, snapshots and binaries are
  *routine*: they are counted per group ("N routine files") and never get rows.
- **Groups.** Files are clustered by directory. Clustering starts at top-level directories, splits
  any group bigger than `max(5, files / max_groups)` by its next path component, then merges the
  smallest groups into their parent (and, at the top level, into one "Other changes" group) until
  the count fits the budget. Groups and the files in them are ranked by log-scaled changed lines,
  plus a large boost for sensitive paths, minus a penalty for tests, docs and config relative to
  source; ties break by path.
- **Budget.** `size_budget(n_files, changed_lines)` (non-routine files; 2,000 or more changed lines moves
  up one tier):

  | non-routine files | max groups (nodes) | rows per group | max edges |
  |---|---|---|---|
  | 0-1 | 1 | 12 | 0 |
  | 2-3 | 3 | 12 | 6 |
  | 4-8 | 5 | 12 | 10 |
  | 9-20 | 8 | 12 | 16 |
  | 21-50 | 12 | 10 | 24 |
  | 51-120 | 16 | 8 | 30 |
  | 121+ | 20 | 8 | 30 |

- **Import edges.** Candidate edges come only from import lines visible in hunks, so each has a
  real citation: TypeScript and JavaScript (`import ... from`, `export ... from`, `require()`,
  dynamic `import()`, multi-line imports, relative paths with extension and `index` probing, and
  `@/` or `~/` aliases matched by path suffix), Python (`import a.b`, `from a.b import c`,
  relative imports, matched to changed files by path suffix) and Go (single and block imports
  with aliases, matched to changed directories by path suffix; standard-library roots are
  skipped). Only targets that are other changed files count. A removed import line is kept as an
  `old` side edge; the v1 diagram uses only `new` side edges.
- **Diagram.** One node per group that has new-side hunk lines (extra groups merge into an "Other
  changes" node). Node kind comes from path words (db, migrations, schema, sql: database; queue,
  jobs, workers: worker; web, ui, components, pages, app, frontend, client: client; otherwise
  service). Layers are the longest path over the group import graph, importers first, with
  cycles broken in a fixed order and a cap of 8. Each node cites up to 12 of its biggest
  new-side hunks and lists only files it cites. Each edge is a group-to-group `imports` edge
  that cites the import lines. The description is a plain sentence ("Changes N files in M areas
  (+A/-D)."), flows are empty, and `gaps` say what was left out (routine files, files without
  hunk data, deleted-only areas, merged areas) and that the diagram was generated
  deterministically from file structure and imports. A PR with no citable new-side line, only
  deleted files, or only docs gets no diagram.

`diagnostics/architecture-status.json` records which rung produced the result:
`{"tier": "llm" | "deterministic" | "none", "reason": "<short>", "generated_by": "llm" |
"deterministic" | null, "skeleton": {"files", "groups", "edges", "routine_files"}}`. The reason is why
the model's diagram was not used ("llm output failed validation", "llm returned no diagram",
"llm unavailable", "llm error: <class>"), or why there is none ("docs-only change", "no citable
lines", "deterministic diagram failed: <class>"). Like the rest of `diagnostics/` it stays out of the
ZIP.

## Schema v2 artifact

`scan()` also writes `generated_artifacts/architecture.v2.json` (schema in
`docs/architecture-v2-contract.md`) after the v1 diagram step. ADE does not read it yet, and v1
(`architecture.json`, `diagnostics/architecture-status.json`) is unchanged and runs first, so a v2
failure never touches it. The ZIP carries v2 under its normal path and as a root alias, like v1.

- **Tiers.** `llm`: the model narrates the deterministic draft (one call per group, then one merge
  call) and the result is validated. `deterministic`: the draft itself, when the harness is missing
  or unsupported, no model output applied, or the LLM step failed. `none`: docs-only or no citable
  lines, or even the deterministic write failed; no file is written. `generated_by` in the document
  is `llm` or `deterministic`. An older v2 file is removed at the start of every run, so a failed
  run never uploads a stale diagram. Nothing in the step can fail the scan.
- **Environment.** `PAIRCLI_ARCHITECTURE_V2=0` turns v2 off (default on). `PAIRCLI_ARCH_LLM_GROUPS`
  (default 8, at most 12; 0 means no group calls), `PAIRCLI_ARCH_CONCURRENCY` (default 3, at most 8),
  `PAIRCLI_ARCH_CALL_TIMEOUT` (seconds per call, default 120) and `PAIRCLI_ARCH_DEADLINE` (seconds
  for all calls, default 240). `PAIRCLI_MODEL_SUMMARY`, `PAIRCLI_MODEL_GROUP` and `PAIRCLI_MODEL_MERGE`
  choose the model per stage; unset means the harness default. No model name is hardcoded.
- **Added scan time.** The step starts after v1, so the worst case it adds is about
  `PAIRCLI_ARCH_DEADLINE` (240 s by default) on top of the summary and v1 time; the deterministic
  parts take well under a second.
- **Cache.** Accepted group and merge answers are cached in `<git toplevel>/.paircli/cache/architecture-v2/`
  (keyed by the hunks, harness and model), so a re-scan of an unchanged group makes no call. The
  cache is local and never uploaded.
- **Diagnostics** (local only, excluded from the ZIP): `diagnostics/architecture-v2-status.json`
  (`tier`, `reason`, `generated_by`, `counts` of groups, nodes, edges and steps, and the `llm_calls`
  stats) and `diagnostics/architecture-llm/` (per-call inputs, raw answers, errors and `stats.json`).
  `scan()` logs one line such as `architecture v2: tier=llm groups_llm_ok=5/8 merge=ok
  steps_dropped=1 cache_hits=2`.

This does not change upload paths, capture offsets, publication versioning, or the
separate `/pair` JSON review-brief agent contract.
