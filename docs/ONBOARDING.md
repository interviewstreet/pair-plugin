# Pair CLI onboarding

The Pair plugin owns Pair CLI's source under `cli/` and remains a thin installer at runtime. It puts Pair CLI (`paircli`) on your
machine and gets out of the way; everything else (scanning, redaction,
publishing, PR detection) is done by `paircli`.

Pair can be installed two ways: the plugin (below), or the one-command installer
(`curl -fsSL https://github.com/interviewstreet/pair-plugin/releases/latest/download/install.sh | sh`, see
"Installing without the plugin"). Both end in the same `~/.paircli` and the same hooks. macOS and Linux only;
on Windows, use WSL.

## What happens at session start

When the first Claude Code or Codex session opens after the plugin is
installed (or updated), the plugin's `SessionStart` hook runs
`plugins/pair/hooks/bootstrap.py` in this repository (`hooks/bootstrap.py` inside the installed plugin). Codex asks you to review and trust the hook first.
Installing alone does not run it; start a new session.

1. If `~/.paircli/bin/paircli` is missing or has not migrated to this plugin's release source, a detached background process
   downloads the release manifest
   `https://github.com/interviewstreet/pair-plugin/releases/latest/download/latest.json`
   with `curl`, picks the binary for your OS and CPU, checks its SHA-256 and a
   100 MiB size cap, and installs it atomically. Only https downloads are
   accepted, using your system trust store. A migration marker records the source
   only after a successful install; it is not a signature. Existing credentials,
   capture cursors and publication state are retained. A failed download leaves
   the current binary intact. Concurrent startup installers are serialized.
2. The same process runs
   `paircli setup --harness <claude-code|codex> --plugin-version 0.4.1`.
   Setup is idempotent: it checks for a newer `paircli`, installs the PR hook
   for that harness, and starts device login if you have no valid credential.
   Login can wait for you to approve in the browser; it never blocks the session.
3. The session shows a one-line status that says what to do next:

   | Status | Meaning |
   | --- | --- |
   | `Pair CLI 0.3.0: logged in, hook installed` | Ready. Nothing to do. |
   | `Pair setup started: installing the Pair CLI, then approve the login in your browser; ...` | First session: the install is running in the background. When it finishes, setup opens `https://pair.sh/app/device?user_code=...` in your browser. |
   | `Pair CLI 0.3.0: login pending, hook installed. To finish, approve the login in your browser; the device code and URL are in ~/.paircli/logs/setup-*.log` | Approve the login on pair.sh. If no tab opened, open the URL from that log (the newest `setup-*.log`) and check the code matches. |

   The status is read when the session starts, so it can say `login pending`
   while you are mid-approval. Start a new session, or run
   `~/.paircli/bin/paircli status`, to see the current state.

## Tools the hook checks

The hook looks for the tools Pair needs and puts a one-line reason in the
status instead of failing silently in a log:

- `python3` runs the hook itself, so it is not checked. If it is missing the
  hook cannot run at all, and no status appears.
- `curl` downloads `paircli`. It is checked until the binary is installed and
  migrated to this repository's releases.
  Without it the status reads `Pair setup did not start: curl not found, ...`
  and nothing is started. Install curl and start a new session.
- `gh` (the GitHub CLI, signed in with `gh auth login`) is what `paircli` uses
  to read and comment on PRs. Without it setup still runs and the status ends
  with `Note: gh not found; ...`. The hook checks that `gh` is on `PATH`, not
  that you are signed in.

The hook always exits 0 and finishes well inside its 5 second timeout: the
checks are lookups on `PATH`, and the install and login run in a detached
process.

## Publishing needs GitHub authorization

After you sign in on pair.sh, publishing also needs a GitHub identity on your
Pair account and the Pair GitHub App authorized for the repository. Pair asks
for that on the PR's page, `https://pair.sh/<owner>/<repo>/pull/<n>`. If
`paircli publish` is refused for lack of it, it prints that link.

## How PRs are published

`paircli` owns the agent hooks. After setup, when the agent runs a successful
`gh pr create`, or a `git push` on a branch that has an open PR, the hook
starts `paircli publish <pr-url>` in the background and prints the
`https://pair.sh/<owner>/<repo>/pull/<n>` link. You can also publish by hand
with `/pair owner/repo#N` (or a PR URL or number), which runs
`paircli publish` and replies with the same link.

Each publish scans the PR, captures the new part of the agent session
transcripts, redacts them, packages everything into one ZIP, uploads it to
Pair, and posts or updates a Pair link comment on the PR.

## What is uploaded

- The scan output for the PR head commit: `report.md`, `comment.md`,
  `signals.json`, `authorship.json`, `snapshot.json`, `agent-trace.json` and
  normalized per-session summaries.
- The Claude Code and Codex session transcript ranges that are new since the
  last successful publish, for sessions linked to the PR. Transcripts can contain
  your prompts and the agent's output.
- A `bundle.json` listing the repository, PR, head commit and redaction counts.

Secrets are redacted on your machine before anything is written to the ZIP:
GitHub, `sk-`, AWS and Slack tokens, JWTs, bearer headers, private key blocks,
`key/token/secret/password` assignments, `.npmrc` auth tokens and the values of
your own secret-looking environment variables become `[REDACTED:<kind>]`.
Redaction is best effort; do not rely on it for secrets you would not want
Pair to hold.

No AI-written text is produced locally. The AI review (overview and architecture) is generated by Pair's servers after the upload, so it
arrives asynchronously, usually within minutes. The pair.sh page fills in when
it is ready; `paircli publish --wait` waits for it and notifies you.

## Installing without the plugin

`install.sh` downloads the release manifest, checks the binary for your platform exactly as the plugin's
bootstrap does (manifest schema, revoked list, release URL, SHA-256 and the 100 MiB cap) into a temp directory,
and runs `paircli init --install-self <dir>`. init:

1. prints what it will set up (no prompt);
2. installs the binary in `~/.paircli/bin` under the same lock as the bootstrap, writes the same migration
   marker, and removes the temp directory;
3. installs the PR hooks in every detected agent, and approves Pair's Codex hooks through Codex itself
   (`codex app-server`), only entries that are exactly Pair's. If your Codex can't, it prints the `/hooks`
   step instead;
4. runs the device login (the browser opens only when there is a terminal; `--no-login` skips it);
5. writes `~/.paircli/state/init.json`, which records the agents and the Codex approvals;
6. checks `gh` and tells you if `gh auth login` or installing `gh` is still needed.

init never re-approves a Codex hook you revoked or disabled in `/hooks`, except when you run
`~/.paircli/bin/paircli init` yourself. Run it again at any time; it only fixes what is missing.

On a machine init set up, Pair's own SessionStart hook (Claude Code or Codex) starts
`paircli maintain` in the background at most once every 24 hours (`~/.paircli/state/maintain.json`). It
updates the CLI, installs hooks for agents added since, repairs removed or stale entries, approves Pair's
new or changed Codex entries (never one you revoked), and opens the login if there is no credential. It logs
to `~/.paircli/logs/maintain-*.log`. Machines that only have the plugin never run it.

A `paircli` installed some other way (Homebrew) and set up with `paircli init` is recorded in
`~/.paircli/state/install.json`; the plugin's bootstrap then runs that binary instead of downloading its own.

## Status and logs

- `paircli status` (or `--json`) shows the version, server, login, installed
  hooks, setup state and your recent publications.
- Logs are under `~/.paircli/logs/`: `bootstrap-<timestamp>.log` for the
  install, `setup-<timestamp>.log` for setup (including the login's device code
  and URL), and one log per publish.
- State is under `~/.paircli/state/`: per-PR publication records, the revision
  journal and the session capture cursors.
- If setup failed, fix the reported cause and run `paircli setup` again, or
  start a new session.

## Remove Pair

Uninstall the plugin first if you have it, then run `~/.paircli/bin/paircli uninstall`. Or, by hand:

```sh
~/.paircli/bin/paircli hook uninstall   # remove the PR hooks from every harness
~/.paircli/bin/paircli logout           # delete the stored credential
rm -rf ~/.paircli                       # binary, logs and state
```

Then remove the plugin from Claude Code or Codex. Do this in order: the binary
is needed for the first two commands.
