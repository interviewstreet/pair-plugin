# Review bundle contract

This is the normative contract between the three PoC2 streams: the plugin that submits a
review, the service that builds it, and anything that later reads the result. The JSON
fixtures under `tests/fixtures/bundle/` are the source of truth. Where this prose and a
fixture disagree, the fixture wins; copy the fixtures into your stream rather than
retyping them.

"Must", "must not", and "may" are normative.

## Bundle key

```
<owner>__<repo>__<n>__<head_sha[:12]>
```

- `owner` and `repo` are the two halves of the GitHub `owner/name` slug, split on the
  single `/`. They are used verbatim, with their original case.
- `n` is the PR number in decimal, with no leading zeros.
- `head_sha[:12]` is the first 12 characters of the 40-character head SHA, lowercase hex.
- The separator is exactly two underscores. Owner and repo names may themselves contain a
  single underscore, so a key must be parsed by splitting on `__` into exactly four parts,
  not by splitting on `_`.

Example:

```
interviewstreet__pair-be__62__3453326da379
```

The regular expression a service may use to validate a key:

```
^[A-Za-z0-9._-]+__[A-Za-z0-9._-]+__[0-9]+__[0-9a-f]{12}$
```

The key is the identity of one review of one PR at one head SHA. A new push produces a new
head SHA and therefore a new key; the old key keeps its bundle.

## GCS layout

```
gs://<bucket>/reviews/<key>/
    manifest.json
    brief.json
    brief.md
    evidence.json
    diff.patch
    risky_hunks.json
    sessions/index.json
    sessions/<agent>-<id>.md
    AGENTS.md
    bundle.tar.gz
```

`<agent>` is `claude` or `codex`. `<id>` is that agent's session id.

On the workspace VM the same tree lives at `.pair/review/<key>/`, with identical file
names and identical contents. The VM copy is what gets packed into `bundle.tar.gz`, and
`bundle.tar.gz` is uploaded to the same prefix.

Submitted payloads are archived separately, under their own prefix:

```
gs://<bucket>/payloads/<key>/<ts>.tar.gz
```

`<ts>` is the receipt time in compact RFC3339 UTC, `YYYYMMDDTHHMMSSZ`, for example
`20260928T001200Z`. If two payloads for one key arrive within the same second, the service
must make the name unique by appending `-<counter>` starting at `2`.

### Write order

Within `reviews/<key>/`:

1. `manifest.json` is written first, with `status` `queued`, and rewritten each time a
   section's status changes.
2. The rendered files are written as they are produced.
3. `bundle.tar.gz` is written last, after every file it contains.
4. `manifest.json` is rewritten one final time with `status` `done` only after
   `bundle.tar.gz` has been uploaded successfully.

The invariant this buys every reader: if `manifest.json` says `done`, `bundle.tar.gz`
exists and is complete. The manifest packed inside `bundle.tar.gz` is the `done` manifest,
so the copy inside the archive and the copy at the prefix agree.

On failure the service writes `manifest.json` with `status` `failed` and does not write
`bundle.tar.gz`. Whatever sections finished before the failure stay in place.

## manifest.json

| Field | Type | Rule |
|---|---|---|
| `schema_version` | integer | `1` |
| `key` | string | Matches the key grammar above |
| `repo` | string | `owner/name` |
| `pr` | integer | PR number, > 0 |
| `title` | string | PR title |
| `base_sha` | string | 40 lowercase hex, the merge base of head and the PR's base branch |
| `head_sha` | string | 40 lowercase hex; its first 12 characters are the key's last part |
| `status` | string | One of `queued`, `running`, `done`, `failed` |
| `sections` | object | Section name to `{status, updated_at, error}` |

Every field is required and none may be null.

Each entry in `sections` has exactly three fields:

- `status`: the same enum as the top-level `status`.
- `updated_at`: RFC3339 in UTC with a `Z` offset and second precision, for example
  `2026-09-28T00:12:07Z`.
- `error`: a string when the section failed, and `null` when there is no error. An empty
  string is not allowed; write `null`.

The section names are the five the brief writers produce: `summary`, `look_first`,
`plan_vs_result`, `pushback`, `side_effects`. All five are present in every manifest from
`queued` onward, so a reader never has to handle a missing key.

Top-level `status` relates to the sections as follows:

- `queued`: no section has started. Every section is `queued`.
- `running`: at least one section has started and at least one is not finished.
- `done`: every section is `done`.
- `failed`: the run stopped. At least one section is `failed`, and any section that never
  ran stays `queued`.

`updated_at` on a section is the time its status last changed.

## Ref grammar

Refs are the citation format shared by `brief.json`, `evidence.json`, the session
markdown, and anything the reviewer writes inline. There are five kinds.

| Kind | Form | Example |
|---|---|---|
| `claude:` | `claude:<session_id>:<message_uuid>` | `claude:d060ef66-8a4c-4f21-9c33-7b5e1a02d9f4:8f3a1c2d-5b60-47ae-9d11-0c6e2f4a7b83` |
| `codex:` | `codex:<session_id>:<turn_index>` | `codex:01a08813-3f7d-7c92-b5e1-4a2d6f80c135:12` |
| `hunk:` | `hunk:<path>:<line>` | `hunk:src/pair/service/app.py:118` |
| `file:` | `file:<path>` | `file:src/pair/service/storage.py` |
| `pr:body` | the literal string | `pr:body` |

Rules:

- `claude:` names one message inside one Claude Code session, by the session id and the
  message's UUID.
- `codex:` names one turn inside one Codex rollout, by the rollout's session id and the
  zero-based index of the turn within that rollout.
- `hunk:` names a diff hunk by its path and the hunk's **first line on the new side** of
  the diff, as it appears in `diff.patch`. The path is repo-relative with forward slashes.
  Because a path may contain colons in principle, a `hunk:` ref is parsed by splitting on
  the *last* colon: everything before it is the path, everything after it is the line.
- `file:` names a whole file by its repo-relative path.
- `pr:body` names the PR description. It takes no arguments; `pr:` with anything else is
  not a valid ref.

A ref must not contain whitespace. Session ids and message ids are drawn from
`[A-Za-z0-9._-]`.

## Session files

`sessions/<agent>-<id>.md` is the rendered transcript of one session. Each message in the
file is preceded by a heading that is exactly that message's ref, so any ref elsewhere in
the bundle can be resolved by a text search within the bundle:

```markdown
## claude:d060ef66-8a4c-4f21-9c33-7b5e1a02d9f4:8f3a1c2d-5b60-47ae-9d11-0c6e2f4a7b83

user: Split the review service out of the plugin.
```

`sessions/index.json` lists the sessions in the bundle, one object per session, with at
least `agent`, `id`, `file` (the path relative to the bundle root), and `role`.

## HTTP

Every route requires `Authorization: Bearer <token>`. A missing header, a malformed
header, or a token that does not match returns `401` and no body beyond an error object.
This includes `GET /health`.

### `POST /submit?force=true|false`

- `Content-Type: application/gzip`. The body is the raw `tar.gz` bytes, not multipart and
  not base64.
- `force` defaults to `false` when absent.
- The service must store the payload at `payloads/<key>/<ts>.tar.gz` **before** it
  acknowledges the request. An upload failure there is a failed submit, not a started job.
- `202` with `{"key": "<key>"}` when a job starts.
- `200` with `{"key": "<key>", "status": "done"}` when the key already has a finished
  bundle and `force` is false. No job starts. The payload is still archived.
- `force=true` always starts a job, even for a finished key. The new run overwrites the
  files under `reviews/<key>/`.

A key whose previous run is `queued` or `running` is not finished, so a resubmit without
`force` is not covered by the `200` case; the service may either join the running job or
restart it, but it must return `202`.

### `GET /reviews/{key}`

- `200` with `{"manifest": {...}, "bundle_url": ..., "bundle_token": ...}`.
- `manifest` is the manifest object described above, verbatim.
- `bundle_url` is the plain HTTPS object URL
  `https://storage.googleapis.com/<bucket>/reviews/<key>/bundle.tar.gz`, with no query
  string, and only when `manifest.status` is `done` and `bundle.tar.gz` exists. For
  `queued`, `running`, and `failed` it is `null`.
- `bundle_token` is an OAuth access token for that object, minted from the service VM's
  metadata server when the URL is present, and `null` otherwise. The caller sends it as
  `Authorization: Bearer <bundle_token>`. It is not a signature and it is not limited to
  one object: it is the service account's token and lasts about an hour.
- A key that has never been submitted returns `404`.

### `GET /health`

- `200` when Claude Code is logged in on the host.
- `503` otherwise.

Health is about the Claude login, not about GCS or the queue. A `503` means submitting is
pointless right now.

## Archive safety

These limits are normative and apply to every archive the service reads, which today means
the submitted payload.

A member must be rejected, and with it the whole archive, when any of the following hold:

- Its name is an absolute path.
- Its name contains a `..` path component.
- Its name contains a backslash (`\`).
- It is a symlink, a hard link, a device, a FIFO, or a directory entry used to smuggle one
  of those. Only regular files are accepted; directory entries themselves may be skipped.

Size limits, checked in this order:

- Compressed size at most **50 MiB**, checked against the request body before any
  decompression.
- Member count at most **500**, checked while iterating the archive.
- Expanded size at most **200 MiB** in total, tracked cumulatively during extraction and
  enforced as bytes are written, not by trusting the header sizes.

Rejection is all-or-nothing: a bad member fails the whole submit, and no partial extraction
is kept.

### Payload contents

The submitted `tar.gz` may contain:

- `meta.json`
- the brief directory outputs (`brief.json`, `brief.md`, `evidence.json`, `diff.patch`,
  `risky_hunks.json`)
- raw session JSONL

### Bundle contents

`bundle.tar.gz` must **not** contain raw JSONL. It contains only the rendered bundle files:

```
manifest.json
brief.json
brief.md
evidence.json
diff.patch
risky_hunks.json
sessions/index.json
sessions/<agent>-<id>.md
AGENTS.md
```

The raw JSONL from the payload stays on the VM and in the archived payload. Rendering it
into `sessions/*.md` is what makes it shareable.

## Fixtures

| File | What it pins |
|---|---|
| `tests/fixtures/bundle/manifest-queued.json` | A manifest right after submit |
| `tests/fixtures/bundle/manifest-running.json` | Two sections done, one running, two queued |
| `tests/fixtures/bundle/manifest-done.json` | All five sections done |
| `tests/fixtures/bundle/manifest-failed.json` | One section failed with a non-null `error` |
| `tests/fixtures/bundle/submit-response-accepted.json` | The `202` body |
| `tests/fixtures/bundle/submit-response-duplicate.json` | The `200` resubmit body |
| `tests/fixtures/bundle/review-response-queued.json` | `GET /reviews/{key}` with a null `bundle_url` |
| `tests/fixtures/bundle/review-response-done.json` | `GET /reviews/{key}` with an object URL and a bearer token |

`tests/test_bundle_contract.py` checks the fixtures against the rules above.
