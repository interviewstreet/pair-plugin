# Pair plugin

Claude Code and Codex plugin that installs [Pair CLI](https://github.com/interviewstreet/paircli).
Pair CLI publishes a pull request's evidence, including redacted transcripts of the agent
sessions that built it, to [pair.sh](https://pair.sh), where an AI review is generated
server-side.

## Install

Claude Code: `/plugin install pair --marketplace interviewstreet/pair-plugin`

## What it does

- At session start, `hooks/bootstrap.py` installs `~/.paircli/bin/paircli` if it is missing and runs `paircli setup`.
- `/pair owner/repo#N` runs `paircli publish` and replies with the pair.sh link.

## What it runs, fetches and sends

- **Downloads:** the release manifest and the `paircli` binary for your OS and CPU from the
  Pair CLI GitHub releases, over https with `curl`. The binary's SHA-256 is checked against the
  manifest before it is installed in `~/.paircli/bin`.
- **Runs:** `paircli setup`, which installs a PR hook for your agent and starts a device login in
  your browser. Nothing is uploaded until you approve that login on pair.sh.
- **Uploads:** after login, when your agent creates a PR or pushes to a PR branch, `paircli`
  uploads the PR scan and the new part of the agent session transcripts, with secrets redacted on
  your machine, to pair.sh. Transcripts can contain your prompts and the agent's output.

## Requirements

- macOS or Linux on arm64 or amd64 (Windows is not supported).
- `python3` and `curl`. The GitHub CLI `gh`, signed in, is needed to publish PRs.

See [docs/ONBOARDING.md](docs/ONBOARDING.md) for setup details, exactly what is uploaded, and how to uninstall.
