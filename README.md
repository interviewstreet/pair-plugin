# Pair plugin

Claude Code and Codex plugin with [Pair CLI source](cli/) in this repository.
Pair CLI publishes a pull request's evidence, including redacted transcripts of the agent
sessions that built it, to [pair.sh](https://pair.sh), where an AI review is generated
server-side.

- `hooks/bootstrap.py` runs at session start: installs `~/.paircli/bin/paircli` if missing and runs `paircli setup`.
- `/pair owner/repo#N` runs `paircli publish` and replies with the pair.sh link.

## One repository, same runtime

- `hooks/`, `skills/` and the plugin manifests integrate with the agent harness.
- `cli/` owns the Go command, scanning, redaction, publishing, hooks, updater,
  notification helper and their tests. Its module is `github.com/interviewstreet/pair-plugin/cli`.
- `.github/workflows/` tests both parts and builds CLI releases from `cli/`.

Users still download a precompiled platform binary on first setup. Existing
installations migrate once to this repository's release source; later CLI
updates use the same source. Users do not need Go or a source build. Creating a
PR or pushing a commit continues to run the installed CLI, not recompile it or
download it on every push. Session-start setup checks for updates as before.

## Development

```sh
uv sync --group dev
uv run pytest
cd cli
go test ./...
go test -race ./internal/notify ./cmd/paircli
go vet ./...
go build ./cmd/paircli
```

## Releases

`v*` tags in this repository are CLI releases. The root release workflow tests
the plugin and CLI, builds macOS/Linux binaries for arm64/amd64, verifies their
checksums and download URLs, attests them, and publishes `latest.json`,
`SHA256SUMS` and the four binaries to this repository's GitHub Release.
CLI versions are independent of the plugin manifest version.

See [the migration and rollout checklist](docs/CLI_MIGRATION.md) before enabling
this plugin version. Anonymous installs require public release assets. A source
move alone does not make a private repository's binaries publicly downloadable.

See [docs/ONBOARDING.md](docs/ONBOARDING.md) for setup, what is uploaded, and how to uninstall.
