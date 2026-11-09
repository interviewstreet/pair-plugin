# Pair plugin

Claude Code and Codex plugin that installs [Pair CLI](https://github.com/interviewstreet/paircli).
Pair CLI publishes a pull request's evidence, including redacted transcripts of the agent
sessions that built it, to [pair.sh](https://pair.sh), where an AI review is generated
server-side.

- `hooks/bootstrap.py` runs at session start: installs `~/.paircli/bin/paircli` if missing and runs `paircli setup`.
- `/pair owner/repo#N` runs `paircli publish` and replies with the pair.sh link.

See [docs/ONBOARDING.md](docs/ONBOARDING.md) for setup, what is uploaded, and how to uninstall.

The organization-owned CLI release must be available before rolling out this
plugin version. See [the repository-transfer checklist](docs/CLI_REPOSITORY_TRANSFER.md).
