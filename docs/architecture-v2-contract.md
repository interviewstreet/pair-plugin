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
    "stats": {"files": 1, "additions": 14, "deletions": 3}
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

Only the keys above are allowed. Unknown keys are stripped with a warning before
validation. Compared with v1, `flows` is replaced by `steps`, `layer` and
`episode_ids` are gone, and groups, `claim`, `stats`, `state`, `side` and
`generated_by` are new.

## Who writes what

| Field | Written by |
| --- | --- |
| `title`, `description`, node `label`/`description`/`kind`/`group`/`files`/`evidence`, edge `label`/`claim`/`source`/`target`/`evidence`, step `text`/`when`/`edge`/`nodes`/`evidence`, `gaps` | model, or the deterministic skeleton |
| `generated_by`, `head_sha`, `schema_version` | code |
| node `change`, `state`, `stats`; edge `change`; group `path`, `column`, `order` | **code only**. Model values are ignored and overwritten |
| step `flags` | reserved. Must be `[]`. Flags are built in a separate change |

## Computed fields

- **Node `change`**: from the snapshot status of its `files`. All added: `added`.
  All deleted: `removed`. Any modified or renamed: `modified`. No files
  (context or external node): `unchanged`.
- **Node `state`**: `routine` when `kind` is `routine` (even if its files
  changed), else `changed` when `change` is not `unchanged`, else `context`.
- **Node `stats`**: `files` count, and `additions`/`deletions` summed from the
  `+`/`-` hunk lines of its files.
- **Edge `change`**: from the kinds of the hunk lines its valid citations point at.

  | Cited lines | `change` |
  | --- | --- |
  | Every cited line is context (neither added nor deleted), on either side, or nothing valid is cited | `unchanged` |
  | Added lines, no deleted line; the citations are only new-side added lines | `added` |
  | Deleted lines (old side), no added line; the citations are only old-side | `removed` |
  | Anything else with an added or deleted line (a mix, or changed lines next to context citations) | `modified` |

  So an import line that exists before and after the PR is `unchanged`, not `modified`.
- **Group `path`**: longest common directory prefix of its nodes' files (empty if none).
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

12 groups, 60 nodes, 60 edges, 24 steps, 5 gaps; 15 files and 12 evidence refs
per node; whole document at most 512 KiB. Ids match `[A-Za-z][A-Za-z0-9_-]{0,63}`.
Node `kind` is one of the v1 kinds (`client`, `service`, `database`, `cache`,
`queue`, `worker`, `storage`, `external`) plus `file` and `routine`.

## Tiers

`generated_by` is `llm` for model-written structure and `deterministic` for the
skeleton-only fallback (groups, file rows and import edges, with real hunk
citations). A docs-only PR produces no diagram.

Import edges are file-level only when the import resolves to exactly one
changed file. A Go import names a package (a directory), so it is a file edge
only when that package has exactly one changed non-test `.go` file; otherwise
it is a package-level link that the v1 diagram uses for group-to-group edges
(its text names the package, never a file) and the v2 draft ignores. A Python
or alias import whose suffix matches several changed files gives no edge.
