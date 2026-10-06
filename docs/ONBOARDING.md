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
`gh pr create` calls start the same scan in the background immediately. Scan
artifacts stay local under `.paircli/pr-<number>/`, including `report.md`,
`signals.json`, `comment.md`, `agent-trace.json`, and `snapshot.json`.

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

The release endpoint must return `application/json` at:

`https://downloads.pair.sh/paircli/latest.json`

It must not redirect to another origin. The manifest's binary URLs must use
HTTPS and return the checksum declared in the manifest. A release is not ready
until those public URLs are live and verified.
