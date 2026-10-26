# Architecture v2: overview layer (additive)

The detailed v2 graph (`groups`, `nodes`, `edges`, `steps`) has one row per file or
pattern. That is checkable but long. The **overview** is a short, curated map on top
of it: 8 to 20 components, one-to-three-word runtime edges, and a walkthrough. It lives
in the same document under a top-level `overview` key and never replaces the detailed
graph. Consumers that do not know `overview` ignore it.

Principle (unchanged): the model chooses and narrates, code computes and verifies.

## Shape

```json
"overview": {
  "generated_by": "llm | deterministic",
  "groups": [{"id": "client", "label": "Client", "path": "packages/ade/src", "column": 0, "order": 0}],
  "components": [{
    "id": "notebook-store", "label": "NotebookModelStore", "group": "client",
    "kind": "component", "description": "Holds cells and outputs; trust is copied from the cell.",
    "members": ["notebook-model-store-ts-a1b2c3"],
    "evidence": [{"type": "diff", "path": "packages/ade/.../NotebookModelStore.ts", "start": 10, "end": 40, "side": "new"}],
    "change": "added", "state": "changed", "stats": {"files": 1, "additions": 556, "deletions": 0, "api_added": 9, "api_removed": 0}
  }, {
    "id": "jupyter", "label": "Jupyter server + kernel", "group": "server", "kind": "context",
    "description": "Child process the server starts on localhost.", "members": [],
    "evidence": [{"type": "diff", "path": "apps/server/src/routes/notebook.services.ts", "start": 200, "end": 230, "side": "new"}],
    "change": "unchanged", "state": "context", "stats": {"files": 0, "additions": 0, "deletions": 0, "api_added": 0, "api_removed": 0}
  }],
  "edges": [{"id": "o-spawn", "source": "notebook-service", "target": "jupyter", "label": "spawn, proxy",
             "claim": "NotebookService starts Jupyter on 127.0.0.1 and proxies only allowlisted routes.",
             "change": "added", "evidence": [{"type": "diff", "path": "...", "start": 210, "end": 214, "side": "new"}]}],
  "steps": [{"id": "o-s1", "when": "after", "edge": "o-spawn", "components": ["notebook-service", "jupyter"],
             "text": "NotebookService starts Jupyter on localhost and proxies only allowlisted routes.",
             "flags": [], "evidence": [{"type": "diff", "path": "...", "start": 210, "end": 214, "side": "new"}]}]
}
```

## Who writes what

- **Model (or deterministic fallback)**: component `id`, `label`, `group`, `kind`, `description`,
  `members`, context `evidence`; group `id`, `label`; edge `id`, `source`, `target`, `label`, `claim`,
  `evidence`; step fields as in v2 (with `components` instead of `nodes`).
- **Code only (overwritten)**: `generated_by`, component `change`, `state`, `stats`, and `evidence`
  for member components; group `path`, `column`, `order`; edge `change`; step `flags` (`[]`).

## Rules

- `kind` is `component` (made of detailed nodes) or `context` (something that is not a changed
  file: a process, a database, a service). A context component has `members: []`.
- **Members** are ids of existing detailed nodes. Each detailed node belongs to at most one
  component. A component has 1 to 40 members. Pattern rows can be members.
- **Coverage is computed.** Every detailed node of kind `file` or `pattern` that no component
  claims is collected by code into one computed component `other` ("Other changes", label
  fixed, `kind: component`) so totals always add up. `routine` nodes are folded into `other`
  too. If nothing is left over there is no `other`.
- **Counts**: 1 to 20 components in total including `other`; at most 8 groups; at most 30
  edges; at most 10 steps (aim for 4 to 8).
- **Component evidence**: a component with members takes its evidence from its members (code
  copies up to 12 refs, largest first), so it is checked without the model re-citing. A
  context component must cite at least 1 valid ref (the code in the diff that shows it exists
  or is used) and must be an endpoint of at least one edge.
- **Edge labels** are short: at most 24 characters and at most 3 words, naming the runtime
  action ("join room", "sync", "spawn, proxy", "edit cells"), not "imports" unless that is
  the only relation shown. A claim is at most 300 characters.
- **Every edge and step cites at least 1 valid ref**, with the same side rules as the detailed
  graph (before steps cite old side only, after steps new side only; context lines are
  converted as in v2; invalid refs are dropped; a step with none left is dropped).
- Edge `source` and `target` are component ids, distinct. Step `components` are 1 to 16 existing
  component ids; if `edge` is set, both its endpoints are in `components`.
- After-steps may not use a component or edge whose computed `change` is `removed`; before-steps
  may not use one whose `change` is `added`.
- **Edge change** is computed from the cited lines exactly as for detailed edges.
- Component `change` for a component with members: all members added, `added`; all removed,
  `removed`; else `modified`; if every member is unchanged, `unchanged`. Context: `unchanged`.
- A step that fails validation is dropped without dropping the overview. An overview that fails
  validation is replaced by the deterministic overview. The detailed graph is never affected.

## Deterministic overview (fallback)

One component per detailed group (label from the group), `members` = that group's nodes (a group
with more than 40 members is split by directory), edges = the group-to-group aggregation of
detailed import edges (label `imports`, claim "N imports between these areas", evidence up to 3
sample refs from the underlying edges), no steps. Same limits; groups beyond the cap merge into
`other`.

## Display

Viewers show the overview by default and offer the detailed graph on demand. A component card
shows its label and stats only; the description, members and citations are in the detail panel.
