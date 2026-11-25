# Distributing Pair without marketplace approval

Status: plan, for review. No code changes are part of this document.
Date: 2026-11-25. Repo state: `main` @ `528b55c`.

> **This plan only adds things.** Nothing that exists today is removed or deprecated, and nothing behaves differently for existing users. These all stay
> supported, and keep working as they do now:
>
> - the Pair plugin;
> - the self-hosted Claude Code and Codex marketplace catalogs;
> - `paircli setup`;
> - the `/pair` skill;
> - the plugin's SessionStart bootstrap;
> - the hooks the CLI installs.
>
> The one-line installer and `paircli init` are a **second onboarding path next to the plugin**, not a
> replacement. The two paths share the same binary, state and hooks, so a machine can use either one or both.
> See §5.1.

## TL;DR

- **Everything a user needs is already in `paircli setup`.** It detects which agents are installed, writes the
  hooks, self-updates, and runs the device login. The plugin's job is to download `paircli` and start `setup`.
  So a second path into the same CLI can live next to the plugin without changing it.
- **Neither official directory will take the plugin as it is, whatever happens to Verified status.** Codex's
  docs say outright that plugins with lifecycle hooks can't be listed. On the Anthropic side, policy §1.G
  (no extracting chat history) is a separate risk that Verified status doesn't remove.
- **Recommendation:** add a one-line installer as a second onboarding path, and let the CLI keep itself up to
  date whichever path installed it.
  - A new user on the installer path runs **one command** and clicks **Approve** once in the browser that
    opens.
  - After that, Claude Code and Codex are both capturing evidence, and the install updates and repairs itself.
  - Organizations get a **zero-step** managed rollout.
  - The plugin and its self-hosted marketplace stay a **supported path, long-term**. Both paths are documented
    side by side.
  - The directories become optional "discovery" listings for the plugin later, if they become possible at
    all.

## 1. What is in this repo, and what review blocks

| Path | What it does | Why it's needed | Directory status |
| --- | --- | --- | --- |
| `plugins/pair/.claude-plugin/plugin.json` | Claude Code manifest, plus listing metadata | Identity and listing | Passes |
| `plugins/pair/.codex-plugin/plugin.json` | Codex manifest; points at `skills/` | Identity and listing | Passes as a file. Codex blocks the plugin because it has hooks (see §3.2) |
| `plugins/pair/hooks/hooks.json` | `SessionStart` (startup\|resume) `command` hook that runs `python3 bootstrap.py`, with a 5 s timeout | The only automatic trigger that gets the CLI onto the machine | **Blocked** (Anthropic check 1: command hooks need a Verified org) |
| `plugins/pair/hooks/bootstrap.py` | Detects the harness. Downloads `latest.json` and the platform binary with `curl`, checks the SHA-256 and the release URL, and installs atomically to `~/.paircli/bin`. Writes a migration marker. Starts `paircli setup --harness … --plugin-version …` detached. Prints a one-line status | Install, migration and kick-off of setup | **Blocked** (Anthropic check 2: file outside the allowed list. The public checklist "holds" a hook that runs a non-shell file) |
| `plugins/pair/skills/pair/SKILL.md` | `/pair <PR>` runs `paircli publish` under normal Bash permissions | Manual publishing | Passes on its own. Its purpose ties it to §1.G review |
| `plugins/pair/assets/icon.png`, `README.md`, `LICENSE` | Listing assets | Listing | Pass |
| `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json` | Self-hosted catalogs for Claude Code and Codex | Install without the directory | Not reviewed: self-hosted |
| `cli/` (Go) | `paircli`: `setup`, `hook install/uninstall`, `hook <harness> <event>`, `publish`, `login` (device flow), `update` (self-update), `status`, `doctor` | All of the runtime | Outside the plugin payload. Downloaded at runtime |

### What the CLI already does: the key finding

| Need | Already in the CLI | Where |
| --- | --- | --- |
| Detect installed agents | `installAllHarnesses` checks for `~/.claude`, `~/.codex` (or `$CODEX_HOME`) and `~/.pi/agent`. Skips the ones that are missing | `cli/cmd/paircli/hook.go:131` |
| Install hooks idempotently | Merges its own entries, found by the marker `" hook claude-code "` / `" hook codex "`. Backs up the file once | `cli/internal/claudecode/hooks.go`, `cli/internal/codex/hooks.go` |
| Claude Code hook events | SessionStart, UserPromptSubmit, PostToolUse(Bash), PermissionRequest/Denied, InstructionsLoaded, CwdChanged, PreCompact, Stop, SessionEnd → `~/.claude/settings.json` | `cli/internal/claudecode/hooks.go:41` |
| Codex hook events | Same set (with Interrupt) → `$CODEX_HOME/hooks.json`. **It never writes trust.** It tells the user to approve the hooks in Codex | `cli/internal/codex/hooks.go:4,166` |
| Self-update | Runs as the `update` step of `setup`. Skips Homebrew installs | `cli/cmd/paircli/setup.go`, `cli/internal/selfupdate/update.go` |
| Auth | Device flow. Opens `pair.sh/app/device?user_code=…` with `open`/`xdg-open` | `cli/internal/deviceauth/browser.go` |
| One run at a time | `state.TryLock("setup")`. A second run exits 0 | `cli/cmd/paircli/setup.go` |

**Gaps once the plugin isn't there:**

1. Nothing installs the binary. An installer has to do it.
2. Nothing runs `setup` again. Today the plugin's SessionStart hook is the only thing that triggers
   self-update, and the hooks the CLI installs don't update anything.
3. A harness installed *after* setup is never picked up.
4. Codex hook trust is a manual step. Today Codex users approve **twice**: first the plugin's bootstrap
   hook, then the `paircli` hooks that `setup` writes to `hooks.json`.

## 2. How similar tools get into Claude Code and Codex sessions

Repo files were read on 2026-11-25. The Orca and git-ai Codex trust code was spot-checked directly.

| Tool | Mechanism | User steps | Codex | Updates / auth | Evidence |
| --- | --- | --- | --- | --- | --- |
| **git-ai** | `curl … install.sh \| bash` → binary → `git-ai install-hooks` (detects about 16 agents) → login | **1** | Writes `[hooks]` into `config.toml`. **Computes Codex's trust hash itself** and writes `[hooks.state."…"] trusted_hash`, "so Codex auto-trusts our hooks without TUI approval" (fragile) | Background self-update re-runs the installer. RFC 8628 device flow. Ships MDM scripts | [install.sh](https://github.com/git-ai-project/git-ai/blob/HEAD/install.sh), [src/mdm/agents/codex.rs](https://github.com/git-ai-project/git-ai/blob/HEAD/src/mdm/agents/codex.rs) L330–353, [claude_code.rs](https://github.com/git-ai-project/git-ai/blob/HEAD/src/mdm/agents/claude_code.rs), [hook_installer.rs](https://github.com/git-ai-project/git-ai/blob/HEAD/src/mdm/hook_installer.rs), [mdm/](https://github.com/git-ai-project/git-ai/tree/HEAD/mdm) |
| **Orca** (stablyai) | Desktop app. Installs the `orca` CLI and **re-applies agent hooks on every start** | **1** (install the app) | Writes hooks.json/TOML, then **asks Codex itself to grant trust** through `codex app-server` RPCs `hooks/list` + `config/batchWrite`: "Codex owns hook hash computation" | electron-updater | [codex-app-server-client.ts](https://github.com/stablyai/orca/blob/HEAD/src/main/codex/app-server/codex-app-server-client.ts) L8, L113–140, [managed-agent-hook-startup.ts](https://github.com/stablyai/orca/blob/HEAD/src/main/agent-hooks/installation/managed-agent-hook-startup.ts), [claude/hook-service.ts](https://github.com/stablyai/orca/blob/HEAD/src/main/claude/hook-service.ts) |
| **Superset** | Desktop app. Merges into `~/.claude/settings.json` and `~/.codex/hooks.json`, plus PATH wrappers. Uses marker blocks for TOML | 1 | Writes hooks. No trust grant found | (unverified) | [agent-wrappers-claude-codex-opencode.ts](https://github.com/superset-sh/superset/blob/HEAD/packages/agent-setup/src/agent-wrappers-claude-codex-opencode.ts), [managed-toml-block.ts](https://github.com/superset-sh/superset/blob/HEAD/packages/agent-setup/src/managed-toml-block.ts) |
| **Entire** | `brew`/`curl` → `entire enable` (per repo) → `entire login`. Writes **repo-level** `.claude/settings.json` and `.codex/hooks.json` | ~3, per repo | Never writes trust. Reports untrusted events and sends the user to `/hooks` | brew / installer | [claudecode/hooks.go](https://github.com/entireio/cli/blob/HEAD/cmd/entire/cli/agent/claudecode/hooks.go), [codex/trust.go](https://github.com/entireio/cli/blob/HEAD/cmd/entire/cli/agent/codex/trust.go) |
| **Langfuse plugin** | Self-hosted Claude marketplace. `hooks.json` runs a Python script that reads transcripts | 2 + config | n/a | Marketplace | [Claude-Observability-Plugin](https://github.com/langfuse/Claude-Observability-Plugin/blob/HEAD/README.md) |
| **SpecStory** | `brew` + wrapper `specstory run claude`. Skills from a self-hosted marketplace | 2 + habit change | Codex provider | brew | [getspecstory](https://github.com/specstoryai/getspecstory/blob/HEAD/README.md), [marketplace.json](https://github.com/specstoryai/getspecstory/blob/HEAD/.claude-plugin/marketplace.json) |
| **vibe-kanban** | Launches pinned `npx @anthropic-ai/claude-code` / `codex app-server`. Hooks are passed **per session** over the control protocol | 1, but only for sessions it launches | app-server | npx | [executors/claude.rs](https://github.com/BloopAI/vibe-kanban/blob/HEAD/crates/executors/src/executors/claude.rs), [codex.rs](https://github.com/BloopAI/vibe-kanban/blob/HEAD/crates/executors/src/executors/codex.rs) |
| **Conductor** | Closed-source desktop app. Bundles its own claude/codex binaries | 1 | Bundled | (unverified) | [llms-full.txt](https://www.conductor.build/llms-full.txt) |
| **Crystal / Nimbalyst** | Electron app that spawns `codex exec --json` / claude | 1, own sessions only | Spawned | autoUpdater | [codexManager.ts](https://github.com/stravu/crystal/blob/HEAD/main/src/services/panels/codex/codexManager.ts) |
| **claude-squad** | `curl install.sh`. Wrapper that runs agents in tmux worktrees. No hooks | 1 + habit change | Launches it | installer | [install.sh](https://github.com/smtg-ai/claude-squad/blob/HEAD/install.sh) |
| **ccusage** | `npx`. **Reads transcripts after the fact** from `~/.claude/projects` and `$CODEX_HOME/sessions`. Optional statusline | 0 install, but run by hand | Reads rollouts | npx | [claude paths.rs](https://github.com/ryoppippi/ccusage/blob/HEAD/rust/adapters/claude/src/paths.rs), [codex paths.rs](https://github.com/ryoppippi/ccusage/blob/HEAD/rust/adapters/codex/src/paths.rs) |
| **claude-code-router** | Wrapper `ccr code`, a local proxy via `ANTHROPIC_BASE_URL` | 1 + habit change | Has a provider | npm | [cli.ts](https://github.com/musistudio/claude-code-router/blob/HEAD/packages/cli/src/cli.ts) |

**Patterns:**

1. **The tools with one-step onboarding use an installer that writes user-level hook config**: git-ai, Orca,
   Superset. They detect agents, merge their own entries idempotently, and re-apply on start or after an
   upgrade so the install repairs itself.
2. **Codex trust is the main source of friction.** Tools handle it in one of four ways:
   - compute the hash themselves (git-ai; this breaks if Codex changes its format);
   - **have Codex grant trust through app-server** (Orca; the sturdiest of the four);
   - tell the user to run `/hooks` (Entire, and Pair today);
   - use managed `requirements.toml` hooks, which are trusted by policy.
3. Wrappers and desktop orchestrators only see the sessions they launch. That's wrong for Pair, which has to
   capture whatever sessions the user already runs.
4. None of these tools relies on an official directory to install hooks.

## 3. Official docs and policy (fetched 2026-11-25)

### 3.1 Claude Code and the Anthropic directory

- **Plugin components:** skills, commands, agents, `hooks/hooks.json`, `.mcp.json`, `.lsp.json`, output styles,
  `bin/` (on the Bash PATH), plus experimental monitors and themes.
  `${CLAUDE_PLUGIN_DATA}` survives plugin updates.
  [plugins-reference](https://code.claude.com/docs/en/plugins-reference)
- **Hook types:** `command`, `http`, `mcp_tool`, `prompt`, `agent`. But "`SessionStart` supports only `command`
  and `mcp_tool` hooks", and at launch Claude Code "skips their `mcp_tool` hooks". The docs' own advice for
  launch-time work is a `command` hook. So **no non-command hook can replace the bootstrap.**
  [hooks](https://code.claude.com/docs/en/hooks)
- **Pre-submission checklist:**
  - A hook that runs a non-shell file is "held".
  - Binaries are held.
  - HTTP hooks are accepted unless they send a credential.

  The two "Verified-only" rules from our report are **not on any public page**.
  [pre-submission-checklist](https://claude.com/docs/plugins/pre-submission-checklist)
- **Verified status:** "select **Help and appeals**… **Request verified**. A request needs a paid plan and a
  proven domain, and a person at Anthropic decides it." What it unlocks isn't documented. Appeals go to
  `directory@anthropic.com`. [publish](https://claude.com/docs/directory/publish), [submit](https://claude.com/docs/plugins/submit)
- **Policy §1.G:** "Software must not query or extract data from Claude's memory, chat history, conversation
  summaries, or user-generated or uploaded files…". §1.E: "must not collect extraneous conversation data".
  [Software Directory Policy](https://support.claude.com/en/articles/13145358-anthropic-software-directory-policy).
  `docs/DIRECTORY_SUBMISSION.md` already flags this as an eligibility question that **Verified status doesn't
  answer**.
- **Self-hosted marketplaces skip directory review:** "That review doesn't cover a plugin you add from a
  marketplace URL… add those only from sources you trust." [plugins](https://claude.com/docs/plugins).
  Install can be scripted: `claude plugin install <p> --marketplace owner/repo` "adds the marketplace without a
  confirmation step" (v2.1.292+).
  [plugins/install](https://code.claude.com/docs/en/plugins/install)
- **Third-party marketplaces don't auto-update** by default, and `marketplace.json` can't turn it on.
  [plugins/install](https://code.claude.com/docs/en/plugins/install), [host-marketplace](https://code.claude.com/docs/en/plugins/host-marketplace)
- **Managed settings:** admins push `extraKnownMarketplaces` + `enabledPlugins` through the admin console, MDM or
  `managed-settings.json`. `allowManagedHooksOnly` blocks user and plugin hooks, **except plugins force-enabled
  in managed `enabledPlugins`**. [plugins/org](https://code.claude.com/docs/en/plugins/org), [hooks](https://code.claude.com/docs/en/hooks)
- **Hooks in `~/.claude/settings.json`** are picked up by the file watcher. There's no approval prompt.
  [hooks](https://code.claude.com/docs/en/hooks)
- **OTEL:** opt-in through environment variables. Prompt and tool content are off by default.
  [monitoring-usage](https://code.claude.com/docs/en/monitoring-usage)

### 3.2 Codex

- **The directory explicitly rules out hooks:** "Lifecycle hooks are supported for plugins installed manually in
  Codex desktop. **Plugins containing lifecycle hooks aren't eligible for the public plugin directory.**"
  [developers.openai.com/plugins/build/plugins](https://developers.openai.com/plugins/build/plugins).
  Local MCP submissions are "limited to approved partners".
  [desktop-only](https://developers.openai.com/plugins/deploy/desktop-only).
  Guideline §6.4: "Do not request or reconstruct full conversation histories".
  [plugin-guidelines](https://developers.openai.com/plugins/plugin-guidelines)
- **Hooks:**
  - Locations: `/etc/codex/hooks.json`, `$CODEX_HOME/hooks.json`, a trusted project's `.codex/hooks.json`,
    or `[hooks]` in `config.toml`.
  - Only `command` and `mcp_tool` run.
  - Hooks are on by default. The feature key is `hooks`; `codex_hooks` is a deprecated alias.
  - "Before a non-managed hook can run, Codex requires you to review and trust the exact hook definition…
    recorded against the hook's current hash." A plugin update that changes a script needs a new review.
  - **Managed hooks in `requirements.toml` are trusted by policy.** `allow_managed_hooks_only` exists.

  [learn.chatgpt.com/docs/hooks](https://learn.chatgpt.com/docs/hooks)
- **Config details:** `notify` fires only on `agent-turn-complete`. OTEL is opt-in. Rollouts are in
  `$CODEX_HOME/sessions/`.
  [config-reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- **Not documented:** the `[hooks.state]` trust key format. Orca avoids depending on it by calling app-server.

**What this means:** the Codex directory is closed to Pair's current design **for good**, whatever happens with
verification. The Anthropic directory is blocked today by Verified status, and possibly by §1.G after that.

## 4. Alternatives, ranked by how many steps a new user takes

"Done" means: the CLI is installed, hooks are active in **every installed agent**, the user is logged in, and
evidence publishes on `gh pr create` / `git push`. Step counts assume a new user on macOS or Linux who has
both Claude Code and Codex and is signed in to `gh`. Clicking **Approve** on the pair.sh device page counts as
one step.

| # | Onboarding steps (new user, both agents) | Option | Automatic | Evidence reliability | Review exposure | Maintenance | Claude + Codex |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | **0** (admin does the work) | **Org-managed rollout**: Claude `managed-settings.json` (`extraKnownMarketplaces` + force-enabled `enabledPlugins`) or an MDM script that runs the installer. Codex `requirements.toml` managed hooks | Everything. Codex hooks trusted by policy. Survives `allowManagedHooksOnly` | Full | None (no directory) | A managed config template plus an MDM script | Both |
| B | **1 command + 1 Approve click** (0 clicks with a pre-authorized token, see Phase 3) | **One-line installer + CLI-owned lifecycle**: `curl -fsSL https://pair.sh/install \| sh` | Detects agents, installs hooks, grants Codex trust, device login with browser opened, self-update and repair from the SessionStart hook | Full. Hooks are live in already-running Claude sessions | None (no directory). Security review of the installer by the user's org | Installer script, Codex trust integration | Both |
| C | 3–4: `claude plugin install pair --marketplace interviewstreet/pair-plugin`, new session, Approve, plus `gh` if missing. Codex adds `codex plugin marketplace add …`, `codex plugin add pair@pair`, new session, trust the bootstrap hook, then **trust the paircli hooks again**: **~8 total** | **Self-hosted marketplace (today)** | Bootstrap → setup | Full after trust | None (no directory). Generic plugin warning | Two catalogs, plugin version kept in sync in 4 places | Both, Codex awkward |
| D | Same as C (~3 Claude). Not possible for Codex | **Wait for Verified status** | Same as C, from the directory | Full | **High**: §1.G may still block. Codex never | None while waiting | Claude only |
| E | 2 Claude: install from the directory, then `/pair setup`, which runs the installer under a Bash permission prompt. Codex the same, and listing a skills-only plugin there is uncertain | **Directory-safe slim plugin** (skills/commands only) + `paircli init` installs the hooks | Same as B after the prompt | Full | **Medium–high**: a plugin whose purpose is installing an extractor may still meet §1.G / §6.4. `docs/DIRECTORY_SUBMISSION.md` warns that "Running the same extraction through another tool is not a remedy" | Extra plugin to maintain | Both in principle |
| F | 1 command (same as B), but evidence publishes only when the agent calls a tool | **MCP-only** | None for triggers. The agent has to call `publish` | **Poor**: no automatic PR trigger, `SessionStart` `mcp_tool` is skipped at launch, and a local MCP binary is held for review / limited to partners in Codex | Medium | MCP server | Both, weakly |
| G | 1 command (an installer for a daemon) | **Transcript ingestion with no hooks**: a user-level launchd/systemd agent watches `~/.claude/projects` and `~/.codex/sessions`, and a PR is detected through `gh` polling or a pair.sh GitHub App webhook | Detection and upload | **Medium**: loses hook-time session↔commit linkage (cwd, git state at the prompt), and the timing is fuzzy | None (no directory) | A daemon on 2 OSes, plus server-side matching | Both |
| H | 1 install + changed habit (`pair claude`, `pair codex`) | **Wrapper CLI / Agent SDK** | Per launched session | Misses sessions started outside the wrapper | None (no directory) | Tracking both CLIs' flags | Both |
| I | 2+, setting env vars | **OTEL** | Telemetry only | **Poor**: content is opt-in and partial | n/a | Collector | Both |

**Ranking for the stated goal (fewest manual steps):** A (0, but only where an admin manages the machines) >
**B (1)** > G and H (1 but weaker, or a habit change) > E (2) > C, D (3–8). So B is the default for everyone,
A is the enterprise channel, and C (the plugin) remains a fully supported path, documented side by side with B.
A, B and C all end in the same CLI state, so a user can start with any of them.

## 5. Recommendation

### What a new user does under the recommended path (B)

```text
$ curl -fsSL https://pair.sh/install | sh        ← the only command
Pair: installed paircli 0.6.0 to ~/.paircli/bin
Pair: found Claude Code, Codex
Pair: hooks installed for Claude Code (~/.claude/settings.json)
Pair: hooks installed and trusted for Codex (~/.codex/hooks.json)
Pair: opening https://pair.sh/app/device?user_code=ABCD-EFGH …   ← click Approve (the only click)
Pair: logged in as <user>. Done: your next `gh pr create` or `git push` publishes to pair.sh.
```

What happens automatically:

1. **Binary.** `install.sh` downloads `latest.json` and the platform binary, checks the SHA-256 and the exact
   release URL, and installs atomically. This is the same logic as `bootstrap.py`, ported to POSIX `sh`.
2. **Agent detection.** `paircli init` = `setup` + the additions below. It walks `harnessOrder`. Agents that
   aren't installed are skipped and picked up later (step 6).
3. **Hooks, idempotently.** It merges marker-identified entries and doesn't touch user hooks. Re-running prints
   "already set up" and changes nothing.
4. **Codex trust with no `/hooks` step.** After writing `hooks.json`, it starts `codex app-server` and calls
   `hooks/list`, then `config/batchWrite`, so Codex computes and stores the trusted hash for exactly Pair's
   entries. Then it checks again with `hooks/list`. This is Orca's method. Codex older than the app-server
   trust API falls back to the current message ("approve in `/hooks`"). Pair **never** computes the hash
   itself the way git-ai does. *This changes the CLI's current "never write trust" rule. See Q1.*
5. **Auth with no copy-pasting.**
   - The device flow opens the browser with the `user_code` prefilled, so the user only clicks Approve.
   - If there's no browser (SSH), it prints the URL and a QR code.
   - When the user started from the pair.sh onboarding page while logged in, the command carries a
     single-use, short-lived install token (`… | sh -s -- --token <t>`). The CLI exchanges it, so no approval
     is needed (Phase 3, Q3).
6. **Auto-update and self-repair.** The installed Claude/Codex `SessionStart` hook handler
   (`paircli hook <harness> SessionStart`) starts a detached `paircli maintain` at most once every 24 h. It:
   - self-updates;
   - re-runs `hook install` for **every currently detected** agent (so a Codex installed later gets hooks
     and trust);
   - repairs removed or stale entries;
   - refreshes the hook command after an update.

   It never blocks a session.
7. **Prerequisites.**
   - If `gh` is missing or not signed in, `init` says so and offers `gh auth login --web` inline (another
     browser click, not a copy-paste).
   - `curl` is needed only by the installer.
   - `python3` isn't needed on the installer path. The plugin path still uses it to run `bootstrap.py`.

   *Whether Pair should keep depending on `gh` is Q4.*

### 5.1 Compatibility: what stays, and how the two paths coexist

**What stays, unchanged and supported long-term:**

| Piece | Status |
| --- | --- |
| `plugins/pair/` (manifests, `hooks/hooks.json`, `hooks/bootstrap.py`, `skills/pair`, assets) | Kept. Same install commands, same SessionStart bootstrap, same status line |
| `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json` | Kept. Install identity `pair@pair` doesn't change |
| `paircli setup` (flags, steps, `setup.json`, logs) | Kept. `init` *calls* `setupRun` and adds steps around it. `setup`'s own behaviour doesn't change |
| `/pair` skill → `paircli publish` | Kept |
| CLI hook entries in `~/.claude/settings.json` and `$CODEX_HOME/hooks.json` (events, matcher, command format, marker) | Kept. The installer writes exactly the same entries |
| `~/.paircli/` layout (bin, `release-source.json`, state, locks, logs, credential) | Kept, and shared by both paths |
| Manual uninstall sequence in the README | Kept, and still valid |

**One machine, two paths.** Both paths end in the same place:

- the same binary at `~/.paircli/bin/paircli`;
- the same migration marker (`release-source.json` with the same `MANIFEST_URL`);
- the same state directory;
- the same hook entries.

So the order a user takes them in doesn't matter:

- **Plugin first, then installer.**
  - `install.sh` sees a binary that has already migrated and is up to date, so it doesn't download it again
    (the same check as `uses_plugin_release`).
  - `init` → `setupRun` re-merges the hooks. This is a no-op: there's still exactly one Pair entry per event.
  - The login is already valid, so it's skipped.
  - The user ends up with what they already had, plus Codex trust (if Q1 is approved).
- **Installer first, then plugin.**
  - `bootstrap.py` sees the binary and the marker that `install.sh` wrote, so it downloads nothing.
  - It runs `paircli setup --harness <h>`, which is the same no-op merge.
  - The only new thing is the plugin's own bootstrap hook. In Codex the user reviews it once, which is the
    normal cost of the plugin path.

**Who owns what when both are present:**

- **The CLI owns every capture and publish hook**, on both paths: the entries tagged `" hook claude-code "` /
  `" hook codex "` in the user's agent config. `InstallHooks` first strips every entry carrying the marker,
  then appends exactly one per event (`stripOurHooks` / `installEvent` in `cli/internal/claudecode/hooks.go`
  and `cli/internal/codex/hooks.go`). That holds whichever path ran it, and however often.
- **The plugin owns only its bootstrap hook** (`hooks/hooks.json` → `bootstrap.py`). That hook never
  publishes. It only installs and starts `setup`.
- So there's never a second set of publishing hooks.

**No double uploads:**

1. Only the CLI's PostToolUse hook triggers a publish, and there's one of those entries per agent.
2. Even if two triggers fire, for example a Claude session and a Codex session pushing the same PR,
   `paircli publish` takes a per-PR lock. A second run leaves a pending marker instead of uploading, and a
   rerun whose head is already published does nothing (`cli/cmd/paircli/publish.go:215`, `:363`).
3. Setup runs one at a time (`state.TryLock("setup")`). The new `maintain` takes the same lock, and skips if
   `setup.json`'s `updated_at` is under 24 h old. So a plugin session start and a `maintain` run don't both
   update.

**Coexistence requirements the new code must meet (Phases 1–2):**

- `install.sh` installs only to `~/.paircli/bin/paircli`, and writes the same `release-source.json` that
  `bootstrap.py` checks. If the bootstrap didn't recognise the binary, it would download a second copy.
- Hook install must stop rewriting the file when nothing changed, and keep Pair's group where it is.
  - Today `installEvent` strips Pair's group and re-appends it at the end.
  - Codex keys trust by `<file>:<event>:<group>:<index>`. If a user hook comes after Pair's, a no-op re-run
    moves Pair's group to a new index and asks for a new review.
  - This doesn't change *which* hooks are installed. It only avoids needless rewrites, so plugin sessions and
    `maintain` can both run setup safely.
- `paircli init` must not remove or edit the plugin's bootstrap hook.
- `bootstrap.py` and `setup` keep working against a CLI that has `init` and `maintain`. `init` is purely
  additive: a new subcommand.
- **Homebrew installs (Phase 3 only).** A brew binary doesn't live in `~/.paircli/bin`, so today's bootstrap
  would install a second copy.
  - Before offering the tap, add an optional `~/.paircli/state/install.json` that records the
    installer-managed binary path.
  - `bootstrap.py` uses that path when the file exists, and behaves exactly as today when it doesn't.
  - This is the only plugin change in the plan, and it's additive.
- Tests: `tests/test_bootstrap.py` gains "binary installed by `install.sh` → no download, setup runs".
  `cli/internal/{claudecode,codex}/hooks_test.go` gain "a second install leaves the file unchanged
  byte for byte" and "mixed install order → one Pair entry per event".

**Uninstall, per path:**

| Goal | Steps |
| --- | --- |
| Remove the plugin, keep Pair working (switch to the installer path) | `claude plugin uninstall pair@pair` / `codex plugin remove pair@pair`. The CLI hooks, login and capture keep working. After Phase 2, `maintain` keeps the CLI updated; before that, re-run the installer or `paircli update` |
| Keep the plugin, "remove the installer" | Nothing to remove. The installer leaves no state of its own beyond what the plugin path uses. Delete `install.json` if Phase 3 added it |
| Remove Pair completely | 1. Uninstall the plugin **first**. Otherwise its bootstrap reinstalls Pair at the next session start. 2. Run the new `paircli uninstall` (Phase 1). It does `hook uninstall` (every harness) + `logout` + removes `~/.paircli`. If it detects that the plugin is still installed (`claude plugin list` / `codex plugin list` when those are on PATH), it stops and prints the exact plugin uninstall command; `--force` overrides. The existing three-command README sequence keeps working |

### Phases and concrete repo changes

**Phase 0: unblock now (days). No user-facing change to the plugin.**

- Reply on Intercom #159791597 and use **Help and appeals → Request verified** in the portal. Send the
  existing §1.G question from `docs/DIRECTORY_SUBMISSION.md` to `directory@anthropic.com`. Neither of these
  is on the critical path.
- README: add the single-command form `claude plugin install pair --marketplace interviewstreet/pair-plugin`
  (v2.1.292+) next to the existing two-command plugin install. The existing commands stay.
- Add `docs/ORG_ROLLOUT.md` with a `managed-settings.json` snippet (`extraKnownMarketplaces` +
  `enabledPlugins: {"pair@pair": true}`) for HackerRank's own engineers. That's the zero-step path for us
  right away.

**Phase 1: one-command installer (1–2 weeks). This adds a path next to the plugin.**

- `install.sh` (repo root). POSIX `sh`, `set -eu`. It:
  - detects the platform;
  - verifies the manifest (schema, revoked list, exact release URL) and the SHA-256 (`sha256sum` or
    `shasum -a 256`);
  - installs atomically to `~/.paircli/bin`, writing `release-source.json`;
  - runs `exec ~/.paircli/bin/paircli init "$@"`.

  Flags it passes through: `--yes`, `--no-login`, `--token`, `--harness`. It's safe to pipe: everything is
  wrapped in a `main` function that's called on the last line.
- `.github/workflows/release.yml`: attach `install.sh` and its checksum to each release. Serve
  `https://pair.sh/install` as a redirect to `releases/latest/download/install.sh` (pair.sh owners, Q5).
- `tests/test_install_sh.py`: run the script against a fake manifest server (checksum mismatch, revoked
  release, unsupported platform, re-run is a no-op).
- `cli/cmd/paircli/main.go`: add an `init` command, which is `setupRun` with TTY progress output. Without a
  TTY it exits non-zero with clear reasons. `setup` stays as is for the plugin.
- `cli/cmd/paircli/uninstall.go` (new): the full uninstall in §5.1, with the plugin-present check.
- `README.md`, `docs/ONBOARDING.md`: show the two supported paths side by side in "Install", "One command"
  and "Plugin". The existing plugin instructions stay as they are. Add the coexistence and uninstall notes
  from §5.1.

**Phase 2: the CLI owns the lifecycle (2–3 weeks).**

- `cli/internal/codex/trust.go`: app-server trust grant, version-gated with fallback, plus `doctor` output.
  Update the package comment in `cli/internal/codex/hooks.go:4` once Q1 is decided.
- `cli/internal/claudecode/hooks.go` and `cli/internal/codex/hooks.go`, `SessionStart` case: start a
  throttled, detached `paircli maintain`, with a timestamp in `~/.paircli/state/maintain.json`.
- `cli/cmd/paircli/maintain.go` (new): update → `installAllHarnesses` → trust, logged to
  `~/.paircli/logs/maintain-*.log`.
- `cli/internal/selfupdate`: re-install hooks after a successful update.
- `cli/internal/claudecode/hooks.go` and `cli/internal/codex/hooks.go`: write only when something changed, and
  keep the position of Pair's group (§5.1 coexistence requirements). Same entries, same events.
- `plugins/pair/hooks/bootstrap.py`: no change. It keeps working for every plugin user. The plugin and the
  installer write the same idempotent state, so a user who has both is fine (§5.1).
- `cli/internal/deviceauth`: QR or plain URL when there's no browser. Prefill is already there.

**Phase 3: zero-step channels (after Phase 2).**

- Onboarding page on pair.sh that shows the command with an embedded single-use install token. This needs a
  server endpoint and a `paircli login --token` exchange.
- `mdm/` directory with Jamf/Intune/Kandji scripts that run `install.sh --yes --token-file …`. Also a Codex
  `requirements.toml` managed-hooks template and a Claude `managed-settings.json` template, both in
  `docs/ORG_ROLLOUT.md`.
- Optional Homebrew tap (`brew install interviewstreet/tap/pair && pair init`). That's 2 steps, for users who
  prefer brew. `selfupdate` already leaves brew installs alone. It ships together with the additive
  `install.json` lookup in `bootstrap.py` (§5.1), so a machine with both brew and the plugin has one binary.

**Phase 4: directories, only if policy allows.**

- If Anthropic grants Verified status **and** confirms §1.G eligibility, list the existing plugin as a
  discovery path. It shares state with the installer.
- Don't submit a "slim" plugin built to route around the hook check (option E) unless Anthropic and OpenAI
  confirm it's acceptable. Otherwise it reads as the evasion that `DIRECTORY_SUBMISSION.md` rules out.

### What we don't recommend

- **MCP-only (F)** and **OTEL (I)**: they don't capture automatically.
- **Wrapper (H)**: it changes user habits and misses sessions started outside it.
- **Hook-free daemon (G)**: keep it as a possible fallback for users who refuse hooks, but it captures less
  precisely and costs more to build.

## 6. Open questions (product, copy, policy: not decided here)

1. **Codex trust grant.** May `paircli init` grant Codex hook trust through app-server, on the basis that
   running the installer is the user's consent? That removes a step, but it reverses the current
   "only Codex, after the user reviews" rule. If yes, should `init` show a confirmation (`[Y/n]`, skipped by
   `--yes`) that lists the hooks being trusted? Security sign-off is needed.
2. **Consent and disclosure copy.** What should the installer print before it installs hooks that upload
   transcripts? Is a single summary line enough, or do we need an explicit confirmation? There are also
   wording questions for the "Done" line and for the `gh` prompt.
3. **Pre-authorized install tokens.** Are we OK with a short-lived, single-use token in a shell command,
   which ends up in shell history? What TTL? Does it bind to the GitHub identity?
4. **The `gh` dependency.** Keep requiring `gh auth login`, or have pair.sh's GitHub App do the PR reads and
   comments server-side so the CLI doesn't need `gh`?
5. **Hosting and brand.** Who owns `pair.sh/install` (a redirect on the web app)? Is the user-facing command
   `pair` or `paircli` (that is, add a `pair` symlink)?
6. ~~**Plugin's future.**~~ **Resolved: keep the plugin long-term.** It remains a supported path next to the
   installer, with no deprecation in any phase. Existing `pair@pair` users need to do nothing.
7. **Directory strategy.** After Verified status, do we still want a directory listing, given the §1.G risk?
   Who owns the conversation with Anthropic and with OpenAI?
8. **Windows.** Still out of scope?

## Sources

- Anthropic: [plugins](https://claude.com/docs/plugins) · [pre-submission checklist](https://claude.com/docs/plugins/pre-submission-checklist) · [submit](https://claude.com/docs/plugins/submit) · [directory/publish](https://claude.com/docs/directory/publish) · [Software Directory Policy](https://support.claude.com/en/articles/13145358-anthropic-software-directory-policy) · [plugins-reference](https://code.claude.com/docs/en/plugins-reference) · [hooks](https://code.claude.com/docs/en/hooks) · [plugins/install](https://code.claude.com/docs/en/plugins/install) · [plugin-marketplaces](https://code.claude.com/docs/en/plugin-marketplaces) · [host-marketplace](https://code.claude.com/docs/en/plugins/host-marketplace) · [plugins/org](https://code.claude.com/docs/en/plugins/org) · [plugins/security](https://code.claude.com/docs/en/plugins/security) · [monitoring-usage](https://code.claude.com/docs/en/monitoring-usage) · [statusline](https://code.claude.com/docs/en/statusline)
- OpenAI: [build plugins](https://developers.openai.com/plugins/build/plugins) · [desktop-only](https://developers.openai.com/plugins/deploy/desktop-only) · [submission](https://developers.openai.com/plugins/deploy/submission) · [plugin guidelines](https://developers.openai.com/plugins/plugin-guidelines) · [hooks](https://learn.chatgpt.com/docs/hooks) · [config reference](https://learn.chatgpt.com/docs/config-file/config-reference) · [plugins](https://learn.chatgpt.com/docs/plugins)
- Tools: see the links in §2.
- This repo: `plugins/pair/hooks/bootstrap.py`, `plugins/pair/hooks/hooks.json`, `cli/cmd/paircli/setup.go`, `cli/cmd/paircli/hook.go`, `cli/internal/claudecode/hooks.go`, `cli/internal/codex/hooks.go`, `cli/internal/selfupdate/update.go`, `cli/internal/deviceauth/browser.go`, `docs/DIRECTORY_SUBMISSION.md`.
