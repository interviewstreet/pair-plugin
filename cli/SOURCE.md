# Imported source

Imported from `https://github.com/interviewstreet/paircli` main at
`6a7e25f2bc4e11dd34315dbc4acc63f13a90032e` (2026-11-12 inspection), preserving its existing license in `LICENSE`
and third-party notification executable, icon, license and provenance records.
The complete source commit can be resolved from the old repository's Git log.

Import adaptations are the module/import namespace
`github.com/interviewstreet/pair-plugin/cli`, release repository URLs, one remote
parser test expectation, and monorepo build/development paths. The minimum
plugin version for new CLI release manifests is 0.4.0. The original Go module
minimum remains 1.22; no new Go dependency is added. Workflows now live at the
plugin root so GitHub Actions executes them.

The vendored notification assets are byte-identical to the source commit.
Their upstream license and pinned hashes are documented in
`internal/notify/assets/README.md`. They were not downloaded anew or regenerated
for this move.
