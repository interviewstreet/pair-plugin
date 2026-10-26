# Architecture artifact v2 (additive, draft)

Written as `generated_artifacts/architecture.v2.json` beside the unchanged v1
`architecture.json` until ADE reads it. Principle: the model narrates, code
computes. Every claim is cited and validated against the PR snapshot.

## Shape

```json
{
  "schema_version": 2,
  "generated_by": "llm | deterministic",
  "head_sha": "<40 hex>",
  "title": "...", "description": "...",
  "groups": [{"id": "api", "label": "API", "path": "src/api", "column": 0, "order": 0}],
  "nodes": [{
    "id": "reviews-ts", "label": "reviews.ts", "group": "api",
    "kind": "service", "description": "what changed",
    "files": ["src/api/reviews.ts"],
    "evidence": [{"type": "diff", "path": "src/api/reviews.ts", "start": 12, "end": 25, "side": "new"}],
    "change": "modified", "state": "changed",
    "stats": {"files": 1, "additions": 14, "deletions": 3, "api_added": 1, "api_removed": 0, "after_return": 0}
  }],
  "edges": [{
    "id": "e1", "source": "reviews-ts", "target": "db-ts", "label": "insert",
    "claim": "Saves a validated review.", "change": "modified",
    "evidence": [{"type": "diff", "path": "src/api/reviews.ts", "start": 20, "end": 20, "side": "new"}]
  }],
  "steps": [{
    "id": "s1", "when": "after", "edge": "e1", "nodes": ["reviews-ts", "db-ts"],
    "text": "The API now validates a review before saving it.", "flags": [],
    "evidence": [{"type": "diff", "path": "src/api/reviews.ts", "start": 20, "end": 20, "side": "new"}]
  }],
  "gaps": []
}
```

Only the keys above are allowed, plus `pattern` on a `pattern` row (see Pattern rows; it is
required there) and on a `routine` row (optional there, see Routine rows); it is stripped with a
warning from any other row. Unknown keys are stripped with a
warning before validation. Compared with v1, `flows` is replaced by `steps`, `layer` and
`episode_ids` are gone, and groups, `claim`, `stats`, `state`, `side` and
`generated_by` are new.

## Who writes what

| Field | Written by |
| --- | --- |
| `title`, `description`, node `label`/`description`/`kind`/`group`/`files`/`evidence`, edge `label`/`claim`/`source`/`target`/`evidence`, step `text`/`when`/`edge`/`nodes`/`evidence`, `gaps` | model, or the deterministic skeleton |
| `generated_by`, `head_sha`, `schema_version` | code |
| node `pattern` | the deterministic draft; the model cannot change it (patches never touch `kind`, `pattern`, `files` or `evidence` of a pattern row) |
| node `change`, `state`, `stats`; edge `change`; group `path`, `column`, `order` | **code only**. Model values are ignored and overwritten |
| step `flags` | reserved. Must be `[]`. Flags are built in a separate change |

## Computed fields

- **Node `change`**: from the snapshot status of its `files`. All added: `added`.
  All deleted: `removed`. Any modified or renamed: `modified`. No files
  (context or external node): `unchanged`.
- **Node `state`**: `routine` when `kind` is `routine` (even if its files
  changed), else `changed` when `change` is not `unchanged`, else `context`.
- **Node `stats`**: `files` count, `additions`/`deletions` summed from the
  `+`/`-` hunk lines of its files, `api_added`/`api_removed` (see API surface stats) and
  `after_return` (see After-return hint). For a pattern row, and for a routine row that carries a
  `pattern`, every figure covers all `pattern.members`, not just the sample in `files`.
- **Edge `change`**: from the kinds of the hunk lines its valid citations point at.

  | Cited lines | `change` |
  | --- | --- |
  | Every cited line is context (neither added nor deleted), on either side, or nothing valid is cited | `unchanged` |
  | Added lines, no deleted line; the citations are only new-side added lines | `added` |
  | Deleted lines (old side), no added line; the citations are only old-side | `removed` |
  | Anything else with an added or deleted line (a mix, or changed lines next to context citations) | `modified` |

  So an import line that exists before and after the PR is `unchanged`, not `modified`.
- **Group `path`**: longest common directory prefix of its nodes' files (all members for a
  pattern row; empty if none).
- **Group `column` / `order`**: layered layout. Build the group graph from node
  edges (source group to target group, ignoring self edges). Break cycles
  deterministically. `column` is the longest path from a source group, capped
  at 8. `order` is the position within the column, sorted by path then id.
- **Before view**: not stored. Consumers derive it: hide nodes and edges whose
  `change` is `added`, show those that are `removed`. Steps use `when` to say
  which view they belong to.

## Evidence

`{type: "diff", path, start, end, side}`; `side` is `new` or `old` (default `new`).

- `new`: `path` must be a changed file and `start..end` inside one new-side hunk range.
- `old`: `start..end` must be inside one old-side hunk range of that file. For a
  renamed file, `path` may be the new path or its `old_path`.
- A step's refs must match its `when`; see Steps.
- A node needs at least one ref, and every path in `files` needs a ref. A node
  with `kind` `routine`, a `moved` pattern row (its members are verified by the snapshot alone, see
  Moved rows), or an `unchanged` or external node that is an endpoint
  of an edge with valid evidence, may omit evidence.
- Every edge and every step needs at least one valid ref.

## Steps

- `when` is `before` or `after`. `nodes` has 1 to 16 existing node ids.
- If `edge` is set, it names an existing edge and both its endpoints are in `nodes`.
- An `after` step may not use a node or edge whose `change` is `removed`. A
  `before` step may not use one whose `change` is `added`.
- A `before` step cites the old code and an `after` step the new code:
  `before` evidence must be `side: "old"`, `after` evidence `side: "new"`.
  Enrichment moves refs that sit on the wrong side when that is exact: a ref
  whose lines are all context lines is re-expressed on the other side with the
  matching line numbers (a run that is not contiguous there becomes several
  refs). A ref that touches an added line cannot support a `before` step, and
  one that touches a deleted line cannot support an `after` step, so it is
  dropped with a logged note. A step left with no ref is invalid and dropped.
  Validation rejects any step ref on the wrong side.
- `text` is at most 400 characters. `flags` must be `[]`.
- A step that fails validation is dropped (with a logged warning) without
  dropping the graph. A graph that fails validation falls to the next tier.

## Limits

12 groups, 60 nodes, 60 edges, 24 steps, 5 gaps; 15 files (12 sample files on a pattern
row; 15 on a routine row, which may list at most 500 members) and 12 evidence refs per node; whole document at most 512 KiB. Ids match `[A-Za-z][A-Za-z0-9_-]{0,63}`.
Node `kind` is one of the v1 kinds (`client`, `service`, `database`, `cache`,
`queue`, `worker`, `storage`, `external`) plus `file`, `routine` and `pattern`.

## Overview (optional, additive)

The document may carry one more top-level key, `overview`: a short, curated map of 8 to 20
components on top of the detailed graph, with its own shape, computed fields and limits. It is
specified in [architecture-v2-overview.md](architecture-v2-overview.md) and validated by
`paircli_overview.validate_overview`, which `validate_v2` calls (so a document that carries a
bad overview is invalid, and the pipeline never attaches one). Consumers that do not know it
ignore it. The 512 KiB limit covers the whole document, overview included.

## Tiers

`generated_by` is `llm` for model-written structure and `deterministic` for the
skeleton-only fallback (groups, file rows, pattern rows and import edges, with real hunk
citations). A docs-only PR produces no diagram.

Import edges are file-level only when the import resolves to exactly one
changed file. A Go import names a package (a directory), so it is a file edge
only when that package has exactly one changed non-test `.go` file; otherwise
it is a package-level link that the v1 diagram uses for group-to-group edges
(its text names the package, never a file) and the v2 draft ignores. A Python
or alias import whose suffix matches several changed files gives no edge.

## Example

`docs/examples/architecture-v2.sample.json` is the deterministic output (skeleton, deterministic
draft, `finalize_v2`, the deterministic overview attached, `generated_by: "deterministic"`) for a small
synthetic PR (a TypeScript page, a Go gateway and a Python service). It carries the api stats and the
`overview` layer and validates under `validate_v2` against that PR's snapshot. It is not a model
output: its `steps` are empty.

## Pattern rows (additive)

Many PRs repeat one change across dozens of files (a rename, a deleted template, a
version bump, a logging line). One row per file is noise, so code groups them.

A node with `kind: "pattern"` stands for several files:

```json
{"id": "api-legacy-deleted", "label": "40 files: deleted", "group": "legacy", "kind": "pattern",
 "description": "...", "files": ["src/legacy/area0/module0.ts", "..."],
 "evidence": [{"type": "diff", "path": "src/legacy/area0/module0.ts", "start": 1, "end": 18, "side": "old"}],
 "pattern": {"family": "same-change", "count": 40, "signature": "9f2c4a1b7e3d5a60",
             "members": ["src/legacy/area0/module0.ts", "..."]}}
```

- `pattern.family` is `same-change` (identical change signature), `tests`, `docs`,
  `config` (files of that category in the group; used when a group has at least 3),
  `remainder` (see Remainder rows) or `moved` (see Moved rows). A fifth value, `routine`, is the
  family of `kind: "routine"` rows only (see Routine rows): a pattern row never uses it and a
  routine row never uses another.
- `pattern.members` lists every member path (at most 500; larger sets are split by
  directory into several pattern rows). `count` equals `len(members)`, at least 3 (at least 1 on
  a routine row), and every member is a changed file. Members are unique across all rows that have members (pattern rows and routine
  rows), and a member is never listed in the `files` of any other row. `pattern.signature` exists only for
  `same-change` rows; on a `remainder`, `moved` or `routine` row the key is an error.
- **Cluster scope.** `same-change` clustering runs over the whole PR (every non-routine file with
  line data), not per directory, so one change repeated across a flat layout (`packages/pkg00/package.json`
  ... `packages/pkg39/package.json`) is one row. Each cluster is assigned to ONE group, the group that
  holds the most of its members (ties: the smallest group id), and the row appears only there; the
  members' own groups show no row for them, and a group left with no rows is dropped. A row whose
  members span several directories says so in its `description` ("Spans N directories."), and the
  group's computed `path` is the common directory of all members of the rows in it (empty when there
  is none). `tests`, `docs` and `config` folding stays per group.
- **Group names.** A group that keeps real single-file rows keeps its directory label. A group
  whose rows are all pattern rows with members in more than one directory is named after the change
  ("Repeated changes", "Renamed and edited files", "Repeated deletions", "Repeated additions",
  "Test files", ...; a second group with the same name gets " (2)"), never after one member's
  directory. Group ids stay the directory slugs, so they remain stable and unique.
- `files` is a sample of at most 12 members; each needs a citation, as for any node (a `moved` row is exempt).
  `evidence` has at most 12 refs and must cover the sample (the validator requires a valid
  citation of every sample file).
- **Same-change signature** (computed by `change_signature` in `hooks/paircli_patterns.py`):
  the sorted sets of net added and net deleted lines, ignoring comments, plus the file status.
  Each changed line is trimmed and loses a trailing line comment (`//` for ts/js/go/java/c/rs and
  similar, `#` for py/sh/rb/yaml/toml/Makefile, `--` for sql/lua; only when the marker follows
  whitespace and sits outside quotes, so `https://x` and `"a // b"` are kept); blank and pure comment
  lines (`/* ... */`, `*` continuation lines) are dropped; unknown extensions are not stripped. Net means
  `added - deleted` and `deleted - added` as sets, so a line edited only in its comment cancels out and
  how often a line repeats is ignored; then every run of digits becomes `#` (a version bump is still a
  change). A file whose net change is empty has no signature and never joins a same-change row.
  Context lines are ignored. The validator recomputes the signature of every member and rejects the
  row if any differs. So "these N files have the same change" is checked by code, not asserted by
  the model.
- **Labels.** `pattern_label` makes the row label from computed facts, at most 80 characters. A
  `same-change` row whose files are edited (not wholly added or deleted) names its most distinctive net
  changed line:

  ```
  16 files: changes `logger.debug("renamed module", …`
  8 files: adds `warn("old");`
  5 files: removes `legacy();`
  35 files: renamed, edits imports of `../new/shared`
  40 files: deleted
  ```

  The verb is `adds` (net added lines only), `removes` (net deleted only) or `changes` (both); when every
  net line is an import it is `adds imports`, `removes imports` or `edits imports`, followed by `of` and the
  module. The snippet is the longest net added line (else the longest net deleted one) that has a letter or
  digit and is not an import, taken from the first member, cut to 36 characters with an ellipsis (shorter if
  the label would pass 80), backticks replaced by quotes. Digits are shown as `#` unless every member
  carries the snippet exactly as written (the signature ignores digits, so members differ in them). When
  two same-change rows would have the same label apart from their count, each avoids the lines of the other
  when choosing, so two repeated changes that differ by one added line get different labels. Whole-file
  rows keep the status label (`40 files: deleted`, `80 files: added`). Other labels: `12 test files`,
  `6 doc files`, `3 config files`, `8 other changed files`, `12 other files in components/editor`,
  `15 files: moved, not edited`,
  `164 routine files`. A label is a code-written name, not evidence.
- `tests`/`docs`/`config` members must classify into that category.
- Computed fields cover all members: `stats.files` = `count`, additions/deletions summed
  over members, `change` from member statuses, `state` `changed`.
- Edges may end at a pattern row. A set of import edges from one row to many members of
  the same pattern collapses into one edge, labelled `imports (N)`, whose claim states
  how many members it covers (computed) and whose evidence samples up to 3 of the import
  lines. Edges between two pattern rows collapse likewise. No edge is drawn to or from a
  `tests`, `docs` or `config` pattern row.

## Moved rows (additive)

A file that was only moved has status `renamed`, a non-empty `old_path` different from `path`, and no
added or deleted line (a 100% similarity rename: the diff has no hunks). It cannot be cited, so without
this family it would drop out of the diagram, although it is the most important fact about it: it still
imports its old neighbours and exports its old names, unlike an edited rename.

```json
{"id": "src-core-moved-d2222d", "label": "15 files: moved, not edited", "group": "src-core", "kind": "pattern",
 "description": "15 files were moved to a new path with no content change (from src/old/ to src/core/).",
 "files": ["<up to 12 sample members>"], "evidence": [],
 "pattern": {"family": "moved", "count": 15, "members": ["..."]}}
```

- Three or more pure moves in the PR form `moved` rows (fewer stay in the "no usable hunk data" gap).
  Clustering is PR-wide like `same-change`: the whole set is one cluster (split by directory above 500
  members) and each cluster is assigned to one group by the same rule (most members, ties the smallest
  group id). A moved file is in no `tests`/`docs`/`config` fold. A routine file never joins one.
- The validator verifies every member from the snapshot alone (`verify_pattern`): status `renamed`,
  `old_path` present and different from `path`, no added or deleted line, 3 to 500 unique members, in no
  other row. So the row needs no diff citation: it is exempt from the evidence requirement and from the
  "every `files` entry needs a citation" rule; `files` is a sample of at most 12 members without refs
  (it may carry refs, valid or not-needed ones). No edge is drawn to or from it.
- The description names the directories computed from the old and new paths: the most common
  (old directory, new directory) pair, introduced with "for example" when the members do not all share
  it, and "renamed within X/" when both directories are the same. Nothing but the count is a number.
- Computed fields: `stats.files` = count, additions/deletions 0, `change` `modified` (a move is a
  change), `state` `changed`. A group of only moved rows spanning several directories is named "Moved files".
- Models are told that moved-only files are unchanged in content and keep their old names and imports,
  and must verify that by reading a sample file at its new path.

## Routine rows (additive)

A `kind: "routine"` row stands for the routine files (lockfiles, generated, vendored or binary) of one
group. It is pattern-like so that nothing is undercounted: it carries `pattern: {"family": "routine",
"count": N, "members": [...]}` with ALL the group's routine files (no `signature`), and `files` stays a
sample of at most 15 members. A routine file whose directory has no group, or whose group has no rows,
goes to the first group. `stats`, `change` and ownership are computed over all members exactly as for
other pattern rows (`state` stays `routine`); the validator requires members that are changed routine
files (they must classify as lockfile, generated, vendored or binary), at most 500 per row (a bigger set is
split by directory into several routine rows, each taking a node slot), unique across all rows, and
`files` inside `members`. A routine row needs no evidence. `pattern` is optional on a routine row only
so that older documents stay readable: then its figures cover `files`. A pattern row never has family
`routine`, and a routine row never another family.

## Accounting (additive)

`accounting(document, snapshot)` in `hooks/paircli_architecture_v2.py` returns `(covered, total,
missing_paths)`, independent of the validator: a changed file is covered when some node lists it, as a
`pattern.members` entry (pattern rows and routine rows) or in `files`. The invariant of a draft: the sum
over all rows of `stats.files` plus the files in the "no usable hunk data" gap (and any "diagram is full"
gap) equals the PR's changed-file count, so `missing_paths` holds only files that no row could cite and
that are not pure moves of three or more. Overview totals add up for the same reason.

## Remainder rows (additive)

Rows and patterns are limited, but a diagram that silently omits changed files is wrong. After the
file rows and pattern rows are chosen, every non-routine changed file that has no row and is in no
pattern goes into a `pattern` row with `family: "remainder"`:

```json
{"id": "src-remainder-3fa91c", "label": "369 other changed files", "group": "src", "kind": "pattern",
 "description": "369 further changed files, listed in the details; they were not given their own rows. ...",
 "files": ["<up to 12 largest members>"], "evidence": ["<a citation for each sample>"],
 "pattern": {"family": "remainder", "count": 369, "members": ["...", "..."]}}
```

- `members` are 3 to 500 changed files, unique across all rows, sorted. There is no `signature`
  (a validator error if the key is present). A remainder row may only hold files that no other row or
  pattern holds, so with the existing ownership rule every changed file is in at most one row.
  Members may include files without hunk data; at least one sample must be citable.
- Up to 12 leftover files make one row (`8 other changed files`). More are split into one row per
  directory cluster so the overview can place them: each file walks up from its own directory to the
  deepest directory (counting its subtree) that holds at least 3 leftover files, else stops at its
  top-level directory, and each cluster of 3 or more becomes a row labelled `12 other files in
  components/editor` (at most 80 characters; a directory over 40 characters is shortened to its last
  segments behind `...`; the repository root reads `at the repository root`). Clusters of fewer than 3
  files merge into one final `N other changed files` row. There are at most 12 remainder rows: when
  there would be more, the smallest clusters join the final row first, and the draft falls back to one
  row if the node budget is still short. A cluster over 500 files is split along its sub-directories. A
  row lands in the group that owns most of the shown rows' files under its directory, else in the
  group of most of its members. One or two leftover files are not a remainder: they get ordinary file
  rows beyond the group budget. The draft reserves one node slot per remainder row before it budgets
  file rows, so the 60-node cap holds. The split is deterministic.
- Computed fields cover all members, like any pattern row. No edge is drawn to or from a remainder row.
- The draft's gaps report the remainder count; the "not listed individually because of the row
  limit" gap no longer exists for files that sit in a remainder row.

## API surface stats (additive)

`stats` also carries `api_added` and `api_removed`: how many exported names the node's
files add or remove, counted from added and deleted lines and then netted per file by exact name (for a
pattern row, over all members). Exported means: TypeScript/JavaScript `export` declarations (`export function`,
`class`, `const`, `let`, `type`, `interface`, `enum`, `default`, and `export { a, b }` names,
`export * from` counts as one); Python top-level `def`/`class` without a leading underscore;
Go top-level `func`, `type`, `var` and `const` whose name starts with an uppercase letter
(methods included). Net means: a name that appears on both sides of one file is unchanged. A comment-only edit of an
export line (`export function f() {}` becomes `export function f() {} // reviewed`), a changed
signature or an `export { a, b }` list that keeps its names count 0 added and 0 removed; a renamed export
is one removed and one added name. Names are compared exactly, digits are not normalised (`version1` to
`version2` is a real rename, unlike the change signature, where a digit run becomes `#`). Counts are
lexical, they do not prove a name is used elsewhere. The validator recomputes them. Every node carries both numbers (0 when nothing is exported);
documents written before they existed are recomputed by `enrich_v2`, but a document without
them fails `validate_v2`.

## After-return hint (additive)

`stats.after_return` is the number of added or deleted lines that directly follow a return-like line.
It is a lexical hint, never proof that a line cannot run. For ts/tsx/js/jsx/mjs/cjs/py/go/java/rs/rb/kt/
cs/php/swift files, per hunk and per side (an added line is read in the new-side sequence of context and
added lines, a deleted line in the old-side sequence), a changed line counts when the last code line
before it (blank lines and comment lines skipped) starts with `return`, `throw`, `raise`, `break` or
`continue` and has exactly the same leading whitespace, and the changed line itself does not open a new
branch (it does not start with a closing bracket, `end`, `else`, `elif`, `except`, `finally`, `catch`,
`case`, `when`, `rescue`, `ensure` or `default:`). Only the line that directly follows counts, not the
rest of an unreachable block, and the first line of a hunk never counts (nothing precedes it). It is
summed over pattern and routine members and the validator recomputes it like `api_added`. Models may
say that N changed lines sit after a return or throw and may never run when `after_return` is above 0 and
they verified it in the file; they never state it otherwise.

## Row quality rules for generators

- Descriptions lead with what changed in behaviour, not with line counts.
- Tests, docs and config are folded into `tests`/`docs`/`config` pattern rows when a group
  has several. Edges to them are not drawn.
- Steps (3 to 8) must show a flow or a before/after contrast. A step that only restates a
  row, or says tests or docs were added, is not wanted.
