---
name: pair
description: Publish a GitHub PR with the agent sessions that built it to Pair and return the pair.sh review URL.
disable-model-invocation: false
argument-hint: owner/repo#N | PR URL | PR number
effort: low
model: haiku
---

Run this skill only after the user explicitly asks to publish a pull request to Pair. The pull request is `$ARGUMENTS`.

1. Normalize the argument to what `paircli publish` accepts:
   - `owner/repo#N` becomes the URL `https://github.com/owner/repo/pull/N`.
   - A PR URL or a bare PR number is passed through unchanged.
2. Run `paircli publish "<normalized argument>"` with Bash, using the harness's normal permission checks. If permission is denied, stop; do not bypass it. If the shell says `paircli` is not found, run the same command as `~/.paircli/bin/paircli publish ...` (the plugin installs the binary there and may not put it on PATH). Don't run any other command, and don't read the diff, transcripts or any output files.
3. If the command fails, reply with its one-line error and stop. If the error says login is required, tell the user to run `~/.paircli/bin/paircli login`.
4. Otherwise your whole reply is the review URL, with no other text: `https://pair.sh/<owner>/<repo>/pull/<n>`. Take owner, repo and n from the PR URL, or from the `review_url` or PR URL in the command output when the argument was a bare number.

The AI review is generated on the server after the upload, so the page may show it a few minutes later.
