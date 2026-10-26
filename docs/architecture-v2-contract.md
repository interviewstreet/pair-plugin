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
    "stats": {"files": 1, "additions": 14, "deletions": 3, "api_added": 1, "api_removed": 0}
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
required there and stripped with a warning from any other row). Unknown keys are stripped with a
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
  `+`/`-` hunk lines of its files, and `api_added`/`api_removed` (see API surface stats).
  For a pattern row every figure covers all `pattern.members`, not just the sample in `files`.
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
  with `kind` `routine`, or an `unchanged` or external node that is an endpoint
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
row) and 12 evidence refs per node; whole document at most 512 KiB. Ids match `[A-Za-z][A-Za-z0-9_-]{0,63}`.
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

`docs/examples/architecture-v2.sample.json` is a real output for a small synthetic
PR (a TypeScript page, a Go gateway and a Python service). It was generated by the
production pipeline and validates under `validate_v2`.

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

- `pattern.family` is `same-change` (identical change signature), or `tests`, `docs`,
  `config` (files of that category in the group; used when a group has at least 3).
- `pattern.members` lists every member path (at most 500; larger sets are split by
  directory into several pattern rows). `count` equals `len(members)`, at least 3, and
  every member is a changed file. Members are unique across all pattern rows, and a member
  is never listed in the `files` of any other row. `pattern.signature` exists only for
  `same-change` rows.
- **Cluster scope.** `same-change` clustering runs over the whole PR (every non-routine file with
  line data), not per directory, so one change repeated across a flat layout (`packages/pkg00/package.json`
  ... `packages/pkg39/package.json`) is one row. Each cluster is assigned to ONE group, the group that
  holds the most of its members (ties: the smallest group id), and the row appears only there; the
  members' own groups show no row for them, and a group left with no rows is dropped. A row whose
  members span several directories says so in its `description` ("Spans N directories."), and the
  group's computed `path` is the common directory of all members of the rows in it (empty when there
  is none). `tests`, `docs` and `config` folding stays per group.
- `files` is a sample of at most 12 members; each needs a citation, as for any node.
  `evidence` has at most 12 refs and must cover the sample (the validator requires a valid
  citation of every sample file).
- **Same-change signature** (computed by `change_signature` in `hooks/paircli_patterns.py`):
  the sorted set of the file's distinct added and deleted lines (how often a line repeats
  is ignored), trimmed, with every run of digits replaced by `#`, plus the file status. Context lines are ignored. The validator
  recomputes the signature of every member and rejects the row if any differs. So "these N
  files have the same change" is checked by code, not asserted by the model.
- `tests`/`docs`/`config` members must classify into that category.
- Computed fields cover all members: `stats.files` = `count`, additions/deletions summed
  over members, `change` from member statuses, `state` `changed`.
- Edges may end at a pattern row. A set of import edges from one row to many members of
  the same pattern collapses into one edge, labelled `imports (N)`, whose claim states
  how many members it covers (computed) and whose evidence samples up to 3 of the import
  lines. Edges between two pattern rows collapse likewise. No edge is drawn to or from a
  `tests`, `docs` or `config` pattern row.

## API surface stats (additive)

`stats` also carries `api_added` and `api_removed`: how many exported names the node's
files add or remove, counted from added and deleted lines (for a pattern row, over all
members). Exported means: TypeScript/JavaScript `export` declarations (`export function`,
`class`, `const`, `let`, `type`, `interface`, `enum`, `default`, and `export { a, b }` names,
`export * from` counts as one); Python top-level `def`/`class` without a leading underscore;
Go top-level `func`, `type`, `var` and `const` whose name starts with an uppercase letter
(methods included). Counts are lexical, they do not prove a name is used elsewhere. The
validator recomputes them. Every node carries both numbers (0 when nothing is exported);
documents written before they existed are recomputed by `enrich_v2`, but a document without
them fails `validate_v2`.

## Row quality rules for generators

- Descriptions lead with what changed in behaviour, not with line counts.
- Tests, docs and config are folded into `tests`/`docs`/`config` pattern rows when a group
  has several. Edges to them are not drawn.
- Steps (3 to 8) must show a flow or a before/after contrast. A step that only restates a
  row, or says tests or docs were added, is not wanted.
