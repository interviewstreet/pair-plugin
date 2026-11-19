# Pair

Publish pull requests and the coding-agent sessions that built them to Pair for AI review.

## Setup

Install `pair@pair` from the `interviewstreet/pair-plugin` marketplace in Claude Code or Codex, then start a new session. Review and approve the startup hook when your harness requests it. Python 3 and curl are required for initial installation; publishing also needs GitHub CLI authorization.

In Claude Code:

```text
/plugin install pair --marketplace interviewstreet/pair-plugin
```

In a terminal for Codex:

```sh
codex plugin marketplace add interviewstreet/pair-plugin
codex plugin add pair@pair
```

The startup hook downloads the platform-specific Pair CLI binary from this repository's GitHub Releases, verifies its SHA-256 checksum and size limit, and installs it under `~/.paircli/bin`. The checksum is delivered by the same release publisher; it is an integrity check, not independent proof of publisher trust. The CLI sets up capture and PR publishing hooks and, when needed, starts device login at Pair. Installation runs in the background and retains existing login and publication state.

## Usage and data handling

Run `/pair owner/repo#123` to publish explicitly. The skill uses your harness's normal shell permissions, not a pre-approved shell grant. Once setup is complete, CLI-managed hooks also publish after a successful PR creation or a push to a branch with an open PR.

Publishing reads the selected PR's GitHub metadata and linked agent-session transcript ranges, redacts recognized secrets locally, packages review evidence, uploads it to Pair, and posts or updates a Pair review link on GitHub. Redaction is best effort; review what you share. The CLI uses local Pair credentials and GitHub authorization for these operations.

Only publish repositories and session content you are authorized to share. Linked-session selection and redaction do not establish Anthropic Directory eligibility. This plugin is not approved by or endorsed by Anthropic.

CLI source, tests, release workflows, notification helpers and provenance records remain in the same repository under `cli/`. They are not part of this installable plugin directory; runtime code is downloaded explicitly as described above.

## Example requests

These examples require setup, Pair login, GitHub authorization and an existing PR in your current local checkout. Replace the sample repository and number with a disposable PR you control. They publish evidence and can update the PR's Pair link comment; they are not read-only previews.

- Publish a PR by repository and number: `/pair acme/shop#123`.
- Publish a PR by its full URL: `/pair https://github.com/acme/shop/pull/123`.
- After pushing another commit, update the existing Pair review from that repository: `/pair 123`.

## Support and removal

Run `~/.paircli/bin/paircli status` to inspect setup and publication status. For full onboarding, updates and CLI cleanup instructions, see [the repository documentation](https://github.com/interviewstreet/pair-plugin#readme).

Removing the plugin does not automatically remove CLI-managed publishing hooks or delete evidence already uploaded to Pair. To stop future CLI-managed publishing, run `~/.paircli/bin/paircli hook uninstall`. Run `~/.paircli/bin/paircli logout` to remove the local Pair login. These commands do not request server-side deletion.

Report suspected vulnerabilities privately to **security@hackerrank.com**, following [HackerRank's published disclosure policy](https://www.hackerrank.com/trust/security/). Do not post credentials or raw session logs in public issues. See the [repository security guidance](https://github.com/interviewstreet/pair-plugin/blob/main/SECURITY.md).

[Privacy](https://pair.sh/privacy) · [Terms](https://pair.sh/terms) · [Support](https://pair.sh/support)
