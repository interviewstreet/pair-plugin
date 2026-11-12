# Pair plugin

Claude Code and Codex plugin that installs [Pair CLI](https://github.com/interviewstreet/paircli).
Pair CLI publishes a pull request's evidence, including redacted transcripts of the agent
sessions that built it, to [pair.sh](https://pair.sh), where an AI review is generated
server-side.

## Install

Claude Code: `/plugin install pair --marketplace interviewstreet/pair-plugin`

Codex (manual install, not yet in the public directory): `codex plugin marketplace add interviewstreet/pair-plugin`,
then restart the app and install Pair from the marketplace in the Plugins Directory. Codex asks you to review
and trust the session-start hook before it runs.

## What it does

- At session start, `hooks/bootstrap.py` installs `~/.paircli/bin/paircli` if it is missing and runs `paircli setup`.
- `/pair owner/repo#N` runs `paircli publish` and replies with the pair.sh link.

## What it runs, fetches and sends

- **Downloads:** the release manifest
  (`https://github.com/rutvikchandla3/paircli/releases/latest/download/latest.json`) and the `paircli`
  binary it names, for your OS and CPU, over https with `curl`. The binary's SHA-256 is checked against
  the manifest before it is installed in `~/.paircli/bin`. This download happens only when the binary is missing.
- **Runs:** at session start the hook starts one detached background process. It installs the binary if
  needed and runs `paircli setup`, which installs a PR hook for your agent (a change to your agent's
  hook configuration) and starts a device login in your browser. The hook itself finishes in
  seconds and never blocks your session. Nothing is uploaded until you approve that login on pair.sh.
- **Uploads:** after login, when your agent creates a PR or pushes to a PR branch, `paircli`
  uploads the PR scan and the new part of the agent session transcripts, with secrets redacted on
  your machine, to pair.sh. Transcripts can contain your prompts and the agent's output.

## Requirements

- macOS or Linux on arm64 or amd64 (Windows is not supported).
- `python3` and `curl`. The GitHub CLI `gh`, signed in, is needed to publish PRs.

See [docs/ONBOARDING.md](docs/ONBOARDING.md) for setup details, exactly what is uploaded, and how to uninstall.
