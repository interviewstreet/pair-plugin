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

The plugin builds allowed Markdown links from `snapshot.pr.files`. Each bullet
copies one of those links, followed by ` — ` and its reason. Whole-file and
new-side hunk links point to the captured commit only when its SHA matches the
artifact directory. Deleted files, or files without a confirmed head, link to
the PR diff instead, without invented line numbers. Renames use the new path;
paths in URLs are percent-encoded. These are normal HTTPS links that open on
GitHub in the existing overview renderer. Relative paths and bare code spans
are not used as overview navigation links. No renderer change is needed.

Overview contains Intent, Reading guide, Decisions and Verification. It never
includes a Diagram section or a Mermaid block, even when the separate
architecture artifact is unavailable. Architecture stays in its own view.

The generator reads existing scan evidence and, when available, bounded extracts
of the raw-session manifest's JSONL files. It does not run new verification or
treat instructions in evidence as trusted commands. Existing evidence checks,
read-only harness execution, and atomic summary writes remain in place. Structural
validation enforces the four headings in order, nonempty sections, the word
limit, the three guide groups, the guide caveat, allowed links, a reason per
guide entry, no repeated files, the eight-file guide limit, and no Mermaid blocks.
Invalid output does not replace an existing summary; text is never silently truncated. Grounding,
reading priorities, simple English and the other section-specific limits remain
model instructions, not proof of semantic correctness. The existing evidence
already supplies the paths and ranges; no Pair CLI change is required.

## Diagram display and compatibility

For the separate architecture board, the plugin still makes a second read-only
call to the same Codex or Claude harness to generate
`generated_artifacts/architecture.json`. No separate model key
or AI-pipeline worker is needed. Each call has the existing five-minute timeout.

The JSON uses ADE's v1 artifact contract with diff-only citations and empty
`episode_ids` (the plugin does not generate episodes). Validation binds the head
to both the artifact directory and the snapshot's last PR commit, checks strict
fields, lengths, graph references and connected flows, and verifies citations lie
inside actual new-side PR diff hunks. Citations do not prove a model's semantic
interpretation is correct. Deletion-only or insufficient evidence can produce no
diagram rather than fabricated references.

Code nodes can include optional `methods`, for example `["submitReview()"]`.
The generator is asked to use real function names, one node per important function
when separate connections or navigation targets are needed, and focused diff
citations from that node's files. Validation bounds the list to six unique names
(120 characters each) and requires a matching name in cited new-side code lines;
comment-only lines and names found only in transcripts are not sufficient.
This is a lexical grounding check, not a language parser or proof of a runtime call.
Methods are omitted for non-code components or unsupported names. Existing
artifacts without `methods` remain valid and display their component labels.
The deployed ADE parser must support this optional field before using the new
generator; older strict parsers may reject artifacts containing it.

The writer atomically saves valid output and removes an older diagram before a
retry. Null, invalid, oversized or timed-out diagram output is logged and does not
block the Markdown upload. Symlinked output paths are rejected. The existing ZIP
retains the versioned files and exposes a root `generated_artifacts/architecture.json`
alias alongside `overview.md` for ADE's extracted publisher bundle reader.

The interactive **Overview / Architecture** switch still requires deployment of
the separate ADE architecture integration. Older ADE renders the Markdown and
ignores the extra JSON. Pair BE must preserve the root generated-artifacts path
when extracting the existing ZIP. No backend or AI-pipeline API change is made
here; live S3 extraction is a rollout verification, not covered by unit tests.

This does not change upload paths, capture offsets, publication versioning, or the
separate `/pair` JSON review-brief agent contract.
