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
- **Node `state`**: `changed` when `change` is not `unchanged`, `routine` when
  `kind` is `routine`, else `context`.
- **Node `stats`**: `files` count, and `additions`/`deletions` summed from the
  `+`/`-` hunk lines of its files.
- **Edge `change`**: from the hunk lines its evidence cites. Only old-side
  citations: `removed`. New-side citations that are all added lines: `added`.
  Any other cited lines: `modified`. No cited lines: `unchanged`.
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
- A node needs at least one ref, and every path in `files` needs a ref. A node
  with `kind` `routine`, or an `unchanged` or external node that is an endpoint
  of an edge with valid evidence, may omit evidence.
- Every edge and every step needs at least one valid ref.

## Steps

- `when` is `before` or `after`. `nodes` has 1 to 16 existing node ids.
- If `edge` is set, it names an existing edge and both its endpoints are in `nodes`.
- An `after` step may not use a node or edge whose `change` is `removed`. A
  `before` step may not use one whose `change` is `added`.
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
