# Pair Plugin

Pair builds review briefs from the work behind GitHub pull requests and can publish supporting evidence for review.

## Setup

Install the Pair plugin, approve its Codex hooks, and complete Pair CLI device login. See [the onboarding guide](docs/ONBOARDING.md) for setup and upload details.

Device login uses `https://prod-be.pair.sh` unless `PAIRCLI_SERVER_URL` is set.
If device login returns a 404, verify that the configured backend supports Pair CLI device login.
