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

## Organizations that set `allowManagedHooksOnly`

`allowManagedHooksOnly` blocks user and project hooks. A plugin force-enabled in managed `enabledPlugins`
keeps its own hooks, so Pair's session-start setup still runs. But the PR hooks that setup adds to
`~/.claude/settings.json` are user hooks, so they are blocked and PRs are not published. On such machines,
Pair's PR hooks have to come from managed settings too. This guide doesn't cover that yet.

## Codex

Codex has no managed setting that installs a plugin. Codex users on managed machines install Pair the usual
way (see the [README](../README.md#codex)).

## References

- Claude Code: [Manage plugins for your organization](https://code.claude.com/docs/en/plugins/org),
  [Deploy managed settings](https://code.claude.com/docs/en/managed-settings),
  [`allowManagedHooksOnly`](https://code.claude.com/docs/en/settings-reference#allowmanagedhooksonly),
  [Hooks](https://code.claude.com/docs/en/hooks)
