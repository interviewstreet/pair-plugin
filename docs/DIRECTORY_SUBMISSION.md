# Anthropic Directory submission

The repository is a monorepo. Submit `interviewstreet/pair-plugin` with the plugin subfolder set to `plugins/pair`. The root catalogs for Claude Code and Codex both resolve to that folder. The install identity stays `pair@pair`; CLI releases still come from this repository's GitHub Releases.

## Local checks

```sh
uv sync --group dev
uv run pytest
claude plugin validate .
claude plugin validate ./plugins/pair --profile directory
```

The plugin folder includes its own README, license, PNG icon, manifests, startup hook and publishing skill. CLI source, test data, release scripts and notification binaries are maintained outside the installed directory. The runtime downloader remains explicit and documented; this layout does not remove the need to review the downloaded CLI or its credential access.

The skill does not pre-approve Bash commands. Explicit `/pair` publishing uses the harness's normal permission checks. Automatic setup and capture/publish hooks remain unchanged.

## Eligibility stop: session evidence

Policy section 1.G prohibits extracting chat history, summaries and user files. The current CLI reads linked local session transcripts and publishes redacted ranges. `cli/cmd/paircli/publish.go` invokes `capture.Stage`; `cli/internal/capture/capture.go` reads those ranges, and `cli/internal/publish/bundle.go` packages them. This is a substantive eligibility concern, not a packaging error. User consent, redaction, moving files or removing startup hooks do not establish compliance. Get an authoritative determination from Anthropic before submitting this behavior. Do not mark this requirement satisfied.

The two policy documents do not grant Verified status or approval. The portal separately blocks command hooks and restricted executable files for this organization. Keep both issues visible; do not claim fixing either one resolves the other.

This change does not remove capture, change uploads, convert the CLI to MCP, or silently ship a reduced-functionality plugin. A separate GitHub-only review integration without local hooks or session extraction would require a separately approved design and validation. Running the same extraction through another tool is not a remedy.

## Portal and organization steps

1. Connect GitHub in the same Claude organization used for the submission, using an account with push access to this repository. A connection in another organization is not sufficient.
2. Select `plugins/pair` as the plugin subfolder, push the reviewed commit and re-validate. Check the commit shown in the report; old reports do not reflect local changes.
3. Check eligibility with the organization's owner and Anthropic. The expanded report says Anthropic controls Verified status and currently offers no way to apply. Connecting GitHub does not grant that status. The command-hook restriction cannot be fixed by packaging changes; do not remove or disguise automatic hooks just to pass validation. Custom marketplace distribution is separate from Directory approval.
4. The expanded report identifies `cli/cmd/paircli/publish_test.go` and `cli/internal/e2e/scenario.go` as its two literal-credential findings. At reported commit `30a61ca`, these are synthetic fixtures: the publishing token is a repeated-pattern test constant, and the e2e key is explicitly planted to verify that it never reaches published output. They are outside the proposed installable subfolder. Keep these security fixtures intact rather than weakening redaction coverage or changing their construction to hide them from the scanner. Revalidate the actual subfolder after pushing. These findings do not establish an exposed production key; this is not proof that the entire Git history is secret-free. If any genuine credential is found later, revoke or rotate it and remove it from distributed files.
5. Review holds for the downloaded executable, local credentials and automatic hooks. Supply the CLI source and notification asset provenance in `cli/` for reviewer inspection. The CLI currently verifies release checksums, not an independent signature; do not claim stronger authentication.
6. Review the look-alike listing-name hold with Anthropic. Do not rename the plugin without planning migration for existing users.

Privacy, terms, support and icon metadata are intentional listing fields. Do not delete them simply because an older runtime validator reports them as unknown. Local validation covers only part of the Directory checks; a passing command does not establish submission approval.

## Current report disposition

- The two literal-secret findings identify synthetic CLI test fixtures. Packaging excludes them from installation without disguising or weakening the tests. A few Directory checks can inspect outside the submitted folder, so only a new portal report can confirm their disposition.
- Broad skill shell preapproval is removed. The skill continues to request the harness's normal command permissions.
- The reported restricted Go source is outside the installable folder. Python bootstrap and other local-code restrictions may remain; they are not made compliant by relocating Go files.
- Command hooks remain for current users and remain a Directory blocker under the reported organization gate.
- The broad-shell policy hold has its underlying grant removed. Credential access, latest-release binary downloads, Python script execution and resulting executable review remain disclosed review concerns, not claims of approval.
- The uninspectable bundled notification icon/executable and build-only image references are no longer in the plugin payload. The downloaded CLI still contains notification assets on macOS; review must not be evaded by the packaging change. Provenance and pinned helper hashes are in `cli/internal/notify/assets/README.md`.
- The listing-name look-alike hold remains for a reviewer. Keep `pair@pair` stable rather than forcing another user migration merely to change a warning.
- Icon and legal metadata warnings are intentional listing fields. Do not remove working listing metadata or replace real setup instructions with misleading text to hide download-and-run behavior.
- Keep the existing hook command format until a compatible change is tested on both supported harnesses. Rewriting Python as an opaque shell launcher would not establish eligibility.

## Public listing and owner-supplied information

On 2026-11-19, unauthenticated HTTP checks of `https://www.pair.sh/privacy`, `/terms` and `/support` each returned 307 to `/app/login`; following redirects returned a login page, not the requested public document. The redirect URL also embedded `https://localhost:3000/...`. No public legal/support page was verified. Do not treat a final HTTP 200 on the login page as a successful check. The plugin metadata still references the existing Pair URLs; no alternative policy or new retention promise was invented.

Before submission, owners must:

- Publish approved privacy, terms and support pages without sign-in. State actual collected content, processing purposes, service providers, retention, deletion and contact routes; confirm all third-party policies with the responsible owners.
- Connect GitHub in the submitting Claude organization and verify the submitter has repository push access.
- Provide a restricted reviewer account and disposable sample PR/session data privately. Do not commit reviewer credentials or upload existing employee/customer conversations as sample data.
- Complete the portal's data-handling answers accurately, including automatic publishing and session-evidence uploads. Leave unresolved statements unacknowledged rather than assuming compliance.
- Have an authorized owner acknowledge the legal/compliance terms, distribution and branding rights. The existing license was preserved; this change does not grant new rights on the company's behalf.
- Confirm the real listing surfaces. This implementation needs a local macOS/Linux terminal, Python and CLI setup; do not advertise web, mobile or Cowork support without surface-specific validation. Codex support in the repository is not evidence of support on other Claude surfaces.
- Exercise the README's three sample requests on the reviewer fixture. Local unit/build checks are not an actual model or production publication test.
- Revalidate the pushed commit and selected folder, inspect all remaining findings, and notify Anthropic about material changes if a listing is later accepted.

The README now includes three explicit use cases, data-flow and uninstall disclosures, and the published HackerRank security contact. These aid review but do not override the session-extraction restriction.

## Suggested question for Anthropic

> Pair currently uses a local startup hook to install/update a native CLI and configure PR publishing hooks. On PR creation or push, the CLI reads linked Claude Code/Codex session evidence, redacts recognized secrets, uploads that evidence and PR data to Pair for review, and posts a Pair link on GitHub. Our report blocks local hooks/restricted files for an unverified organization. Separately, policy 1.G appears to prohibit the session extraction at the core of this workflow. Is this workflow eligible for the Directory at all, and is there an approved integration route? We are not treating consent, redaction, manual installation or repository subfolder packaging as an exemption. Please clarify both the hook restriction and the evidence workflow before we submit.

Do not send session contents, account credentials or customer source with this eligibility question. No message has been sent by this change.

Official references: [pre-submission checklist](https://claude.com/docs/plugins/pre-submission-checklist) and [marketplace source paths](https://code.claude.com/docs/en/plugin-marketplaces).

Additional references: [Software Directory Policy](https://support.claude.com/en/articles/13145358-anthropic-software-directory-policy), [Software Directory Terms](https://support.claude.com/en/articles/13145338-anthropic-software-directory-terms), and [HackerRank security contact](https://www.hackerrank.com/trust/security/).
