# Releasing and updates

The CLI is developed in `cli/` inside `interviewstreet/pair-plugin`.
Build and test from that directory:

```sh
git clone https://github.com/interviewstreet/pair-plugin
cd pair-plugin/cli
go build ./cmd/paircli
go test ./...
./paircli --help
```

Users still receive precompiled binaries. They do not need Go. The plugin
bootstrap and CLI self-updater both discover releases at:

`https://github.com/interviewstreet/pair-plugin/releases/latest/download/latest.json`

## Build and publish

From the repository root:

```sh
bash cli/scripts/build-release-artifacts.sh 0.5.0
python3 scripts/check-release-artifacts.py cli/dist/paircli/latest.json
```

This produces macOS/Linux arm64/amd64 binaries and `SHA256SUMS` in
`cli/dist/paircli/0.5.0/`, plus `cli/dist/paircli/latest.json`. Each artifact URL
names the exact version and platform under this repository's release tag.
The build requires Go 1.27 for current macOS loader compatibility. Linux CI
also tests Go 1.22, the module's unchanged minimum version.

Only an authorized maintainer should publish: a pushed `vX.Y.Z` tag runs the
root `.github/workflows/release.yml`, tests both plugin and CLI, builds and
checks all artifacts, creates GitHub build-provenance attestations and uploads
the assets. CLI release numbers are independent of plugin manifest versions.
Reserve this repository's GitHub Releases for CLI versions containing
`latest.json`; discovery uses GitHub's latest-release endpoint.

The default minimum plugin version is 0.4.0 for this repository migration.
The existing `PAIRCLI_MIN_PLUGIN_VERSION`, `PAIRCLI_MINIMUM_VERSION`,
`PAIRCLI_RELEASE_OUTPUT_DIR` and `PAIRCLI_RELEASE_BASE_URL` build overrides
remain available. The production release checker rejects foreign asset URLs,
so a staging mirror must not be published through the production workflow.

## Downloads and migration

The plugin installs to `~/.paircli/bin/paircli`. On the first session after
this plugin update, it also refreshes older installations whose updater still
uses the former repository. Only after successful checksum verification and
atomic replacement does it record the new manifest URL in
`~/.paircli/bin/release-source.json`. A failed migration keeps the old binary
and existing credentials/state intact, and retries on the next session.

Later session-start setup runs the ordinary CLI updater. PR creation/push
hooks still use the installed binary; they do not rebuild or unconditionally
download it. The updater refuses revoked releases, observes plugin
compatibility, and supports `paircli update --check`. Homebrew-managed
executables retain the existing `brew upgrade paircli` behavior; plugin
bootstrap manages only its own path under `~/.paircli/bin`.

Only the CLI updater supports the existing `PAIRCLI_UPDATE_MANIFEST_URL`
override and `PAIRCLI_ALLOW_INSECURE_DOWNLOAD=1` for loopback HTTP tests.
The plugin bootstrap uses its fixed HTTPS endpoint in this repository; it
does not support an environment override or an offline installer. Automated
bootstrap tests inject a local fetch implementation, never publish evidence,
and do not replace the user's installed CLI.

The manifest contains the schema, version, minimum/plugin versions,
revocations and artifact digests. Bootstrap enforces its repository/version/
platform contract, HTTPS, checksums and a 100 MiB cap. The CLI preserves its
existing GitHub release-asset redirect policy. These checks do not make a
checksum an independent signature; clients do not currently verify CI
attestations. Anonymous downloads require the repository/assets to be public.

## Homebrew (optional)

Pair can also be offered as a Homebrew formula in a tap (`interviewstreet/homebrew-tap`, which does not exist
yet). Users then run `brew install interviewstreet/tap/pair && paircli init`. After a release, render the
formula from that release's manifest and commit it to the tap as `Formula/pair.rb`:

```sh
python3 scripts/render-homebrew-formula.py cli/dist/paircli/latest.json > Formula/pair.rb
```

The renderer applies the plugin bootstrap's checks to the manifest. A Homebrew `paircli` doesn't self-update
(`brew upgrade` does), and its hooks call the brew bin link, which survives upgrades. When `paircli init` runs
from a Homebrew keg (or its bin link), it writes `~/.paircli/state/install.json` naming that binary, and the plugin's
bootstrap then uses it instead of downloading a second copy. Without the file, the bootstrap behaves as before.

## Notification assets

macOS binaries retain the existing pinned universal notification executable,
Pair icon and upstream MIT license. Linux builds do not embed them. Setup
requires no additional download or package manager for the bundled helper.
See `internal/notify/assets/README.md` for its provenance and pinned hashes.
From `cli/`, maintainers can refresh it with the existing reviewed script:

```sh
bash scripts/update-notification-helper.sh
```

Review its release, license, architecture support and hashes before committing
changes. macOS tests use temporary homes, not the user's Applications folder.
See [the rollout checklist](../../docs/CLI_MIGRATION.md) before enabling this
plugin version or retiring the old CLI repository.
