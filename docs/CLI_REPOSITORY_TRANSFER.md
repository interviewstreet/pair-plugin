# CLI repository ownership rollout

This is the first ownership-only step: keep Pair CLI in its own repository,
but transfer it to `interviewstreet/paircli`. CLI releases can still update
independently of the plugin. Moving the CLI into this private plugin repository
is deferred; no Go source or compiled binaries are added to the plugin here.

## Release gate

Do not deploy this plugin version until all of the following are confirmed:

1. A source repository admin transfers the existing `rutvikchandla3/paircli`
   repository to `interviewstreet`, retaining the `paircli` name and public
   visibility. Transfer the repository, rather than creating an empty repository
   or copying it: keep its history, releases and open PRs. An organization owner
   may need to approve the public repository under organization policy.
2. Confirm anonymous access to `https://github.com/interviewstreet/paircli` and
   its existing releases. Do not recreate the old personal repository: doing so
   would remove GitHub's transfer redirects used by existing installations.
3. Merge the CLI organization-release URL changes in the transferred repository.
   Confirm its Actions policy allows the release workflow and required build
   attestation permissions. Publish an approved CLI patch release from that repo;
   the release job must refuse to publish under a different owner.
4. Check, without GitHub credentials, that the organization's latest manifest
   and all four referenced platform binaries download successfully. New manifests
   must point to `interviewstreet/paircli/releases/`, not the personal account.
   Keep an approved working release selected as GitHub's latest release.
5. Test fresh installs and existing-plugin upgrades in isolated homes before
   releasing plugin 0.3.1. Preserve login state, publication state and hook paths.

Existing users must update the plugin for the one-time organization-source
refresh. Older CLI updaters restrict release-asset redirects to the personal
repository path, so GitHub transfer redirects alone do not guarantee that they
can download a new manifest's organization-hosted artifact URLs. Plugin 0.3.1
downloads the approved organization binary before invoking setup, then records
the completed migration; subsequent sessions skip reinstallation. No binary,
credential or publication-state directory paths change.

## Trust boundary

Organization ownership gives the organization control of release publishing; it
does not turn the manifest's SHA-256 into independent provenance verification.
The bootstrap still checks a checksum delivered by the same release source.
Independent signature/attestation verification and stronger release protections
are a separate hardening task, not claimed as solved by this ownership move.

If the organization cannot keep the CLI release repository public, stop this
rollout. The current anonymous download path cannot read private GitHub releases;
choose authenticated release access or bundle the binaries in the plugin first.

## Recovery

If the organization manifest or assets are unavailable, do not ship the plugin
URL change. Already installed CLI binaries and their local state remain in place.
Retain a known-good organization-owned release as latest if a new CLI release
must be rolled back; do not silently fall back to a personal release source.
