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
session evidence. Pair also packages the complete commit folder as a sibling
`<head-commit>.zip`, then requests a signed upload from Pair BE and streams the
ZIP directly to S3.
This upload can include local agent session transcripts and prompts in addition
to generated review files; plugin installation should disclose that data flow.
The ZIP contains `.paircli/pr-<number>/<head-commit>/` and no other PRs.
The source artifacts stay local under
`.paircli/pr-<number>/<head-commit>/`, including `summary.md`, `report.md`,
`signals.json`, `comment.md`, `agent-trace.json`, and `snapshot.json`. The
summary covers the actual code change, a Mermaid flowchart, verification steps,
risks, and supporting evidence. The plugin records the summary path or a failure
reason and ZIP path in `scan.json` and the scan log.

Pair CLI reads local session data for the configured harness when it builds Pair
review data. Device login contacts the Pair backend. The current Codex flow
does not post a PR comment. By default, uploads use the CLI login server and
bearer token. The backend records the Pair user who requests the signed S3
form, but does not verify GitHub access to the PR path. Deploy the Pair BE
upload-access change before releasing this plugin version.
Set `PAIRCLI_UPLOAD_ENABLED=0` to disable automatic uploads. On auth or access
failure, the local ZIP is kept and `scan.json` records the failure and log path.
The browser's session cookie is not read or copied by the plugin.

For a private upload test after deploying Pair BE's anonymous upload-signing
change, set `PAIRCLI_UPLOAD_SERVER_URL=https://dhruvi-pair.private.hackerrank.link`
before starting Codex or Claude. This changes only where the plugin requests
`/api/v1/upload/url`; CLI installation and login still use `PAIRCLI_SERVER_URL`.
The signing request sends no CLI token in this mode. Use the origin only,
without `/api` in the setting. The ZIP still goes directly to the S3 URL that
Pair BE returns.

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
