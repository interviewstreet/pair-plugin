# CLI source and release migration

## Scope

The CLI is maintained in `cli/` in this repository, with its existing runtime,
tests, documentation and notification assets. It remains a downloaded native
binary. The plugin does not compile Go code on a user's machine. Scanning,
redaction, API requests, bundle format, PR triggers and the `~/.paircli` data
layout are unchanged by the move.

The source import is based on `interviewstreet/paircli` main commit
`6a7e25f`. See [source provenance](../cli/SOURCE.md). Pending changes from the old
repository are not silently included and must be ported separately.

## Existing installations

On the first session using plugin 0.4.0, bootstrap installs the supported
platform binary from this repository and atomically records its manifest URL
in `~/.paircli/bin/release-source.json`. This also migrates an executable whose
old updater points at the personal repository or the former organization CLI
repository. It does not execute the old updater to perform that migration.

A file lock prevents competing bootstrap installs. A failed download, invalid
manifest, incompatible plugin, unsupported platform, revoked release or checksum
mismatch does not replace the old executable or mark migration complete. The
next session retries. Login, hooks, logs and publication/capture state are not
removed. Once migrated, normal `paircli setup` and `paircli update` use this
repository's release endpoint. No unconditional binary download was added to
PR creation or pushes.

The source marker is only migration bookkeeping. SHA-256 detects corrupted or
mismatched bytes; a checksum served with a binary is not independent publisher
authentication. CI also publishes GitHub build-provenance attestations, but
the current client does not verify those attestations. Repository/release
permissions remain part of the trust boundary.

## Rollout

1. Merge and pass both plugin and CLI CI. Do not enable this plugin release for
   users before its CLI assets are available.
2. Publish a new CLI version from this repository, greater than the latest
   previously shipped CLI (for example `v0.5.0` after `v0.4.5`). Reserve `v*`
   GitHub Release tags for CLI releases; an unrelated latest release without
   `latest.json` would break discovery. Plugin metadata versions are separate.
3. Confirm all four binaries, `SHA256SUMS`, `latest.json` and attestations exist.
   Check the latest manifest resolves under `interviewstreet/pair-plugin`,
   references its exact release/platform, and supports plugin 0.4.0.
4. Check anonymous access to the release assets before enabling external users.
   A private repository returns 404 without authorization. This change does not
   change visibility or add a GitHub token to the download hook.
5. Update the plugin distribution, start a fresh agent session, and verify
   `paircli version`, `paircli status` and the migration marker. Repeat with a
   previously installed CLI. Test publishing only on an explicitly authorized
   test PR; unit tests and release builds do not upload evidence.
6. Port any remaining CLI PRs to `cli/` here before retiring the old repository.
   Stop new releases there only after the new source is verified. Do not delete
   old release assets: users on older plugin versions may still reference them.

This PR does not publish a tag, change repository visibility, retire the old
repository, replace a user's installed CLI, or upload agent transcripts.
