# Pair CLI onboarding

Pair starts installation after the first eligible `gh pr create`. Codex still
requires the user to review and trust the Pair plugin hook before it can run.

## What approval does

Setup downloads the checksum-verified `paircli` binary for the current
platform, installs a Pair hook for **only** the triggering harness, and opens
Pair device login if no valid credential exists. A later pull request checks
for a verified CLI update and re-installs that same harness hook.

The PR that requested approval is queued until setup finishes. Pair then runs
`paircli scan <pr-url> --snapshot` in that repository. Later eligible
`gh pr create` calls start the same scan in the background immediately. The
plugin then runs the triggering Codex or Claude agent in read-only mode to
write an AI-authored `summary.md` from the scan artifacts, PR diff, and linked
session evidence. All files stay local under
`.paircli/pr-<number>/<head-commit>/`, including `summary.md`, `report.md`,
`signals.json`, `comment.md`, `agent-trace.json`, and `snapshot.json`. The
summary covers the actual code change, a Mermaid flowchart, verification steps,
risks, and supporting evidence. The plugin records the summary path or a failure
reason in `scan.json` and the scan log.

Pair CLI reads local session data for the configured harness when it builds Pair
review data. Device login contacts the Pair backend. The current Codex flow
does not post a PR comment or upload a PR artifact yet; its hook reports that
limitation directly.

## Status and recovery

The hook prints the current onboarding and scan status plus log paths. A failed
setup is reported on the next PR instead of silently retrying. The lock includes
a PID and timestamp, and is reclaimed after its owner exits or after 30 minutes.

To retry a failed setup, create another eligible pull request after resolving
the reported error.
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
