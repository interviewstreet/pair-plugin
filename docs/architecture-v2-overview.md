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
    "change": "added", "state": "changed", "stats": {"files": 1, "additions": 556, "deletions": 0, "api_added": 9, "api_removed": 0, "after_return": 0}
  }, {
    "id": "jupyter", "label": "Jupyter server + kernel", "group": "server", "kind": "context",
    "description": "Child process the server starts on localhost.", "members": [],
    "evidence": [{"type": "diff", "path": "apps/server/src/routes/notebook.services.ts", "start": 200, "end": 230, "side": "new"}],
    "change": "unchanged", "state": "context", "stats": {"files": 0, "additions": 0, "deletions": 0, "api_added": 0, "api_removed": 0, "after_return": 0}
  }],
  "edges": [{"id": "o-spawn", "source": "notebook-service", "target": "jupyter", "label": "spawn, proxy",
             "claim": "NotebookService starts Jupyter on 127.0.0.1 and proxies allowlisted routes.",
             "change": "added", "evidence": [{"type": "diff", "path": "...", "start": 210, "end": 214, "side": "new"}]}],
  "steps": [{"id": "o-s1", "when": "after", "edge": "o-spawn", "components": ["notebook-service", "jupyter"],
             "text": "NotebookService starts Jupyter on localhost and proxies allowlisted routes.",
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
  too; a routine row stands for all its routine files (see the contract), so the sum of the
  components' `stats.files` equals the PR's changed-file count minus the files with no usable hunk
  data that the detailed graph reports as a gap. If nothing is left over there is no `other`.
- **Stats** are the five detailed stats plus `after_return` (a lexical count of changed lines
  that directly follow a return, throw, raise, break or continue; see the contract). Component
  stats are the sum of the members' stats, computed by code.
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

## Prose rules (what the model may write)

- **No counts in prose.** Code owns every number (component `stats`, the digest's `count`,
  `stats` and `pattern.directories` fields). The model writes "the repeated edit" or "many files",
  never "111 files" or "+222 -111", and copies a number only from a digest field named `count` or
  `stats`, verbatim: sampled members are not the whole set.
- **Validator guard.** For a model overview (`generated_by: "llm"`), `validate_overview` rejects
  (an ordinary `ArchitectureError`, so the single retry shows it to the model) a component
  description, edge claim or step text that contains a number followed by `files`, `handlers`,
  `modules`, `lines` or `components` (with optional "more") unless the number is a computed count:
  the component's `stats` (or both edge ends', or the step's components'), the `stats` or pattern
  `count` or file count of any of its members, additions plus deletions, the sum of two such member
  counts, or a PR-wide total. Numbers up to 3, numbers inside backticks or quotes, and anything
  the check cannot tell apart pass. The deterministic overview is written by code and not checked.
- **Context nodes are reached through the server.** When a changed server component starts,
  proxies or calls a context component (a process, database, service), the edge goes between the
  server component and the context component (for example `spawn, proxy`), client edges go to the
  server component and never straight to the context component, and the text says the server
  starts it when the diff shows that, citing the lines that start or call it.
- **Trust boundaries get their own component or step** (an allowlist, auth check, sandbox, proxy,
  permission gate) with a plain statement, grounded in cited lines, of what it allows or blocks.
- **Names describe the change.** A pattern row can span many directories; the digest gives each
  `pattern.directories` and `pattern.common_dir`. A component is named for what it does, never for
  the directory of a sample file when `directories` is above 1, and may hold several pattern rows
  of related edits. New pattern families (such as `remainder`) are treated like any other.
- **No narrow "only" claims**: no "only", "just", "all", "every" or "none" about what a file or
  route serves, calls or contains unless the cited lines are its whole definition; prefer
  "includes" and "such as", and call listed endpoints or exports examples.
- **Verified facts or nothing**: statements such as "the imports name something the target does
  not export" need the model to have read the file around the cited lines; when unsure it omits
  them. `after_return` is the only computed hint for dead code: when a node's `stats` carry it
  above 0 the model may say that that many changed lines sit after a return or throw and may never
  run, after verifying it in the file, and never states it otherwise.
- **Moved files**: a node whose `pattern.family` is `moved` stands for files that were only moved
  to a new path. The digest lists it with its family and count; the model says that their content
  is unchanged and that they keep their old names and imports, after reading a sample file at its
  new path.
- **Gaps** (detailed document, merge call): the model writes at most 2 generic limits ("runtime
  behaviour not visible in the diff"), never statements about specific files, directories, areas
  or counts. `apply_merge` drops (with a warning) any model gap containing a path-like token
  (a `/` or a known file extension), a number followed by files/rows/areas/etc., or a name such as
  `area4`; code adds its own computed gaps.

## Deterministic overview (fallback)

One component per detailed group (label from the group), `members` = that group's nodes (a group
with more than 40 members is split by directory), edges = the group-to-group aggregation of
detailed import edges (label `imports`, claim "N imports between these areas", evidence up to 3
sample refs from the underlying edges), no steps. Same limits; groups beyond the cap merge into
`other`.

## Display

Viewers show the overview by default and offer the detailed graph on demand. A component card
shows its label and stats only; the description, members and citations are in the detail panel.
