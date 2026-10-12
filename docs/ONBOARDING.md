# Pair CLI onboarding

Pair starts CLI installation and device login when the first Codex or Claude
session opens after plugin installation. Codex still requires the user to
review and trust the Pair plugin hooks before they can run. Installation alone
does not run a hook; start a new session after installing or updating Pair.

## What approval does

Setup downloads the checksum-verified `paircli` binary for the current
platform, installs a Pair hook for **only** the triggering harness, and opens
Pair device login if no valid credential exists. A later pull request checks
for a verified CLI update and re-installs that same harness hook.
Login targets `https://prod-be.pair.sh` by default. Set `PAIRCLI_SERVER_URL`
before starting Codex or Claude to test a private or local backend. Credentials
are scoped to that backend; switching servers starts a new device login rather
than reusing an unexpired token from the previous server.

If a PR is created before login finishes, its scan is queued until setup
completes. Pair then runs `paircli scan <pr-url> --snapshot` in that repository.
Later eligible `gh pr create` calls start the same scan in the background
immediately. The plugin then runs the triggering Codex or Claude agent in read-only mode to
write an AI-authored `summary.md` from the scan artifacts, PR diff, and linked
session evidence. Pair packages the complete commit folder as a sibling `<head-commit>.zip`.
It reserves evidence with `POST /api/v1/reviews/publications/upload-url`, streams
the ZIP directly to the signed S3 POST, then calls
`POST /api/v1/reviews/publications/:id/finalize`. Only confirmed finalization
reports a successful publication. The request declares canonical PR input,
head SHA, a positive revision, ZIP SHA-256, and exact ZIP size. The backend
verifies GitHub access using the publisher's own identity, then verifies the
uploaded object's size/checksum before marking evidence available and re-arming
review workspace sync. Workspace installation still completes asynchronously.

The ZIP contains a root `overview.md` copied from the generated summary, plus
`.paircli/pr-<number>/<head-commit>/` and no other PRs. The source artifacts stay
local, including `summary.md`, `report.md`, `signals.json`, `comment.md`,
`agent-trace.json`, and `snapshot.json`. The summary is a concise reviewer brief
covering what changed, why, the runtime flow, key implementation files,
verification, and review notes. It uses the evidence internally without
repeating commit metadata, artifact identifiers, raw logs, or a full
changed-file list. Uploads can include local agent session transcripts and
prompts. The plugin does not use browser session cookies or reviewer JWTs;
publishing requires the CLI device-login bearer.

ZIP timestamps and permissions are normalized so unchanged evidence has the
same checksum. A local `.publication-revisions.sqlite3` beside the commit
folders stores only server/head/checksum/size/revision metadata, never tokens
or signed forms. Identical retries reuse a revision; changed content advances
across heads. Concurrent scans serialize revision assignment. An immutable
upload snapshot prevents a later scan from replacing bytes during publication.
A finalized reservation replay skips S3 and confirms finalization idempotently.
A revision conflict from another device is surfaced rather than overwritten.
Keep the local revision journal when retrying a failed upload or finalization.

The current Codex flow does not post a PR comment. Set
`PAIRCLI_UPLOAD_ENABLED=0` to generate artifacts without publishing. Uploads use
the CLI login server and bearer. `PAIRCLI_UPLOAD_SERVER_URL`, if set, must match
the authenticated backend; anonymous upload signing is not supported by this
protocol. Log in to the target backend using `PAIRCLI_SERVER_URL` before
publishing there. On auth/access, pending App permissions, S3, or finalize
failure, the ZIP and revision journal remain local and `scan.json` records a
failure. Backend review routes and the publisher's GitHub App authorization
must be configured before running live publication.

## Status and recovery

The hook prints the current onboarding and scan status plus log paths. A failed
setup is reported on the next PR instead of silently retrying. The lock includes
a PID and timestamp, and is reclaimed after its owner exits or after 30 minutes.

To retry a failed setup, run the approval command shown by the hook after
resolving the reported error. A new session or PR reports the prior failure.
Enterprise users can point bootstrap at an approved HTTPS mirror or pinned
manifest with `PAIRCLI_RELEASE_MANIFEST_URL`; paircli updates use
`PAIRCLI_UPDATE_MANIFEST_URL` for the same purpose.

## Remove Pair

Find the installed binary in the `binary` field of the plugin's
`onboarding.json`, then run the one-command teardown:

```sh
python3 /path/to/paircli_onboarding.py uninstall --harness codex
```

Replace `codex` with `claude-code` or `pi` if that is the harness Pair set up.
The command removes that harness's hook, logs out, and deletes the downloaded
binary and consent metadata. Finally remove the plugin through Codex. If its
plugin data remains, delete the directory that contains `onboarding.json` and
`auth.json`.

## Release endpoint requirements

The release job publishes `latest.json` and each versioned binary as GitHub
Release assets. Bootstrap and later CLI updates use:

`https://github.com/rutvikchandla3/paircli/releases/latest/download/latest.json`

GitHub may redirect that URL to its HTTPS release-asset hosts. Other
cross-origin redirects are rejected. The manifest's binary URLs must use HTTPS
and return the checksum declared in the manifest. GitHub labels release assets
as `application/octet-stream`; Pair accepts that type only after this
repository's expected GitHub redirect. A release is not ready until those
GitHub Release assets are live and verified.
