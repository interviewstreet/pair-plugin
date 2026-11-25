# Rolling Pair out to an organization

For machines an admin manages, Pair can be turned on with no step for the person using the machine. The
admin pushes a managed settings file; Claude Code then installs and enables the Pair plugin by itself, and the
plugin's session-start hook does the rest. The user only approves the Pair login in the browser the first time
a session starts.

Nothing here replaces the per-user install in the [README](../README.md#install). Both end in the same CLI
state.

## Claude Code: managed settings

Put these keys in Claude Code's managed settings: as JSON in the claude.ai admin console, as top-level keys
of an MDM plist (macOS), or in the `managed-settings.json` file:

| OS | `managed-settings.json` location |
| --- | --- |
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux and WSL | `/etc/claude-code/managed-settings.json` |

```json
{
  "extraKnownMarketplaces": {
    "pair": {
      "source": { "source": "github", "repo": "interviewstreet/pair-plugin" },
      "autoUpdate": true
    }
  },
  "enabledPlugins": { "pair@pair": true }
}
```

- `extraKnownMarketplaces` adds this repository's marketplace, named `pair`, so no one runs
  `claude plugin marketplace add`. `autoUpdate` keeps the plugin current; third-party marketplaces don't
  update on their own otherwise.
- `enabledPlugins` force-enables `pair@pair`.
- To pin a version, add a `ref` to the `github` source. To mirror the marketplace in your own repository
  instead, the `extraKnownMarketplaces` key and the `@pair` suffix must both become your marketplace's own
  `name` from its `marketplace.json`. See [Custom marketplaces](../README.md#custom-marketplaces).

If the file already exists, merge these two keys into it rather than replacing it, or put them in their own
file in the `managed-settings.d/` directory next to it.

By default only one managed source applies on a machine: the admin console first, then MDM, then the file. If
your organization already sends any setting from the admin console, keys in an MDM profile or the file are
ignored. Put these keys in the source that already delivers your policy, or set
`"managedSourcesBehavior": "merge"`.

## What the user sees

1. Once the settings reach the machine, Claude Code registers the marketplace and installs the plugin at the
   start of the user's next session.
2. The plugin's session-start hook installs `paircli`, installs the PR hooks and opens the Pair device login.
   If the plugin install finishes after that session has started, this happens in the session after it.
3. They click **Approve** on pair.sh. From then on, `gh pr create`, or a `git push` to a branch with an open
   PR, publishes the PR to pair.sh.

`gh` must be installed and signed in (`gh auth login`) for Pair to read and comment on PRs.

## Installing on managed machines (Claude Code and Codex)

To roll out the one-command installer instead of (or as well as) the plugin, run the script for your platform
from your MDM. It runs Pair's installer as the user with `--no-login`; nothing is uploaded until the user
approves the Pair login, which opens at their first Claude Code or Codex session.

| Tool | Script | How to attach it |
| --- | --- | --- |
| Jamf Pro | [`mdm/macos/install-pair.sh`](../mdm/macos/install-pair.sh) | A policy's **Scripts** payload, run at login or check-in |
| Kandji | [`mdm/macos/install-pair.sh`](../mdm/macos/install-pair.sh) | A **Custom Script** Library item, run once per device |
| Microsoft Intune (macOS) | [`mdm/macos/install-pair.sh`](../mdm/macos/install-pair.sh) | **Devices > macOS > Shell scripts**, run as root |
| Linux fleet tools | [`mdm/linux/install-pair.sh`](../mdm/linux/install-pair.sh) `<user>` | Any tool that runs a script as root |

The macOS script finds the logged-in user and exits 1 when nobody is logged in. Pair's installer also fails
while the user has neither Claude Code nor Codex installed, so run the script as a recurring policy until it
succeeds. Each run is safe to repeat. After it, the machine updates itself daily from Pair's session-start
hook (on machines that block user hooks, once the managed-hook templates below are deployed).

## Organizations that block user hooks

`allowManagedHooksOnly` (Claude Code) and `allow_managed_hooks_only` (Codex) block user and project hooks. A
Claude Code plugin force-enabled in managed `enabledPlugins` keeps its own hooks, so Pair's session-start setup
still runs. But the PR hooks Pair adds to `~/.claude/settings.json` and `~/.codex/hooks.json` are user hooks,
so they are blocked and PRs are not published.

On such machines, deliver Pair's hooks as managed hooks too. Managed hook commands are absolute paths under a
managed directory, so the install scripts above also install a small wrapper,
`/usr/local/share/pair/pair-hook.sh` ([`mdm/templates/pair-hook.sh`](../mdm/templates/pair-hook.sh)), which
runs the user's own `~/.paircli/bin/paircli` and does nothing for a user who doesn't have it yet. Deploy the
wrapper wherever you deploy the templates. Then:

- Claude Code: merge [`mdm/templates/claude-managed-settings.json`](../mdm/templates/claude-managed-settings.json)
  into the managed settings. A file in `managed-settings.d/` works only when the file-based source is the one
  that applies (see the note on managed sources above).
- Codex: merge [`mdm/templates/codex-requirements.toml`](../mdm/templates/codex-requirements.toml) into
  Codex's `requirements.toml`. It sets `managed_dir` to the wrapper's directory. Managed hooks are trusted by
  policy, so there is no approval step.

Both templates set the managed-only key, because without it Pair's own user hooks run as well and every event
is recorded twice. Use them only if your organization wants managed hooks only.

The Codex template follows the `[hooks]` tables, `managed_dir` and absolute-path guidance Codex documents for
`requirements.toml`; it has not been tried against a managed Codex install yet.

## References

- Codex: [Hooks](https://learn.chatgpt.com/docs/hooks) (managed hooks in `requirements.toml`)
- Claude Code: [Manage plugins for your organization](https://code.claude.com/docs/en/plugins/org),
  [Deploy managed settings](https://code.claude.com/docs/en/managed-settings),
  [`allowManagedHooksOnly`](https://code.claude.com/docs/en/settings-reference#allowmanagedhooksonly),
  [Hooks](https://code.claude.com/docs/en/hooks)
