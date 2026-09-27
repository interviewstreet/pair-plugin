# M8 results (Sep 24, 2026)

All six test PRs ran through the real `/pair` skill: `claude -p "/pair <pr>" --plugin-dir <repo>` with `MAX_THINKING_TOKENS=0`, timed from `stream-json` output. Costs are what `claude -p` reported.

Two settings were measured:

- **Default:** the user's own settings, hooks, and MCP servers load. This is what an interactive `/pair` sees.
- **Lean:** `--setting-sources project --strict-mcp-config --no-session-persistence`. This is what the M7 hook runs.

The final six-PR run used default settings. awt#1 was then rerun in lean mode after the pushback fixes described below. A lean rerun of all six stopped after two PRs when the Claude account hit its session limit (see known issues).

## Matched sessions

Every match agrees with the plan's test-PR descriptions.

| PR | Kept sessions (role) | Opened the PR | Excluded (overlap) |
|---|---|---|---|
| five-sh/awt#1 | 6 Claude build: c52ab291, 880302e3, a49144e5, 51e2e625, d060ef66, 03747bd6 | 03747bd6 | none |
| hackerrank#39311 | 9 Claude build: d95d3f86, a0ee6572, f8e028d1, 64d6fbbd, dc72988e, be3070fc, c4c67b87, ab1011cb, ebe6620d; 5 Codex build: 01a07f7f, 01a0802a, 01a080a1, 01a080f9, 01a084fe | d95d3f86, a0ee6572, be3070fc, 01a0802a, 01a084fe | 01a07738 (0.0) |
| ideverse#763 | 2 Claude build: eac43d5a, bc655cbd | both | none |
| hackerrank#39418 | Codex 01a08813 build, 01a086b4 discussion | both | 01a084fe (0.09), 01a087e0 (0.12) |
| workspace-service#757 | Codex 019fd7df build | none | 019fd74f (0.06), 019fda3d (0.0) |
| pair-be#56 | Claude 1b6016cd build (matched by `on_head_branch`; the edits were `sed -i`) | 1b6016cd | none |

- awt#1: the main build session (d060ef66) is not the one that opened the PR (03747bd6), as the plan expects.
- hackerrank#39311: 14 sessions across orca and awt worktrees, despite the rebase. `be3070fc` (the "PR analysis" session) counts as build because it edited PR files.

## Timings

`prepare` took 2.1 to 3.6 s on every PR, which meets the "about 5 s or less" check.

**Default settings, final run** (all six: no denials, no tool errors):

| PR | Prepare (s) | Writers (s) | Summary (s) | Total (s) | Benchmark (s) | Cost ($) |
|---|---|---|---|---|---|---|
| awt#1 | 2.8 | 17.5 / 26.8 / 39.2 | 31.6 | 90.7 | 53 | 0.391 |
| hackerrank#39311 | 3.6 | 8.4 / 25.2 / 26.7 | 31.9 | 75.7 | 71 | 0.447 |
| ideverse#763 | 2.9 | 8.6 / 19.7 / 23.4 | 25.6 | 63.8 | 41 | 0.359 |
| hackerrank#39418 | 2.3 | 12.8 / 25.6 / 32.1 | 34.9 | 86.9 | 43 | 0.298 |
| workspace-service#757 | 3.0 | 4.8 / 5.1 / 11.6 | 16.8 | 46.2 | 28 | 0.191 |
| pair-be#56 | 2.1 | 5.2 / 5.3 (2 writers) | 11.8 | 31.9 | 13 | 0.133 |

**Lean settings:**

| PR | Runs | Total (s) | Benchmark (s) | Cost ($) |
|---|---|---|---|---|
| awt#1, final code | 5 | 53.4 to 60.1 | 53 | 0.19 to 0.23 |
| awt#1, earlier code | 3 | 55.1, 56.1, 76.0 | 53 | 0.19 to 0.35 |
| hackerrank#39311, final code | 1 | 91.3 | 71 | 0.423 |
| hackerrank#39418, earlier code | 1 | 79.7 | 43 | 0.258 |

Against the plan's checks:

- **"On awt#1 the brief is ready in about 60 s or less":** met in lean mode (5 of 5 runs with the final code). Missed with default settings (90.7 s).
- **"Within about 15 s of the benchmark":** met for hackerrank#39311 (default, +4.7 s) and awt#1 (lean, +0.4 to +7.1 s). Elsewhere the gap was 18 to 44 s.

Where the extra time goes:

- Orchestration: the main agent adds 10 to 18 s beyond prepare, the slowest writer, and the summary. The plan's estimate was 6 to 9 s.
- Writer latency varies a lot between identical runs. The awt#1 plan-check writer took 20.4 s in one run and 39.2 s in another, and hackerrank#39418's summary took 27.1 to 34.9 s.

## awt#1 pass criteria

| Criterion | Default, final run | Lean, final code (5 runs) |
|---|---|---|
| `make test-tmux` was never run | yes | 5 of 5 |
| `awt migrate` kills sessions, never tried on v0.1 state | yes | 5 of 5 |
| The layout decision was reversed later | **no** | 5 of 5 |
| The "have you tested it?" pushback | yes | 5 of 5 |
| The reviewer answers design questions from the transcript | yes (M6) | not rerun |

The default run's pushback list filled its 8 slots with smaller corrections and dropped the layout reversal. Also, no decision was marked superseded: the four questions asked in one AskUserQuestion call share a ref, and the model put prompt refs in `supersedes`. Three fixes followed:

- `save` marks the decision among those sharing a ref whose text best matches the pushback item, and drops `supersedes` refs that aren't decisions.
- The pushback prompt fills its list in a fixed order: reversals first, then testing questions, then everything else.
- Each pushback candidate carries `hints` that name the cues found in its prompt: testing words, reversal words, or layout and default words.

With the final code, every lean run put the layout reversal second. It also marked exactly two decisions superseded, and both were right: the pane layout answer and the picker's modal answer.

For the fifth criterion, the M6 reviewer (Sonnet) was asked why the agent pane is on the left. It streamed its first text in 2.6 s, finished in 9.0 s (the benchmark: 9 to 16 s), and cited a turn in d060ef66 and `internal/command/layout.go:104-105`.

## look_first against teammates' line comments

A comment counts as "inside" when a hunk cited by `look_first` covers its line on the same side.

| PR | Run | Cited hunks (files) | Commented files covered | Comments inside cited hunks |
|---|---|---|---|---|
| hackerrank#39311 | default | 14 (13) | 10 of 13 | 31 of 38 |
| hackerrank#39311 | lean | 12 (12) | 8 of 13 | 23 of 38 |
| hackerrank#39418 | default | 8 (8) | 3 of 4 | 15 of 17 |

- ideverse#763, workspace-service#757, and pair-be#56 are merged with no line comments. awt#1 is still open.
- The two hackerrank#39311 runs show that the choice of hunks varies between runs of the same evidence.
- Comments left on older diffs use their original line numbers, so they can land outside the final hunks. The counts are a lower bound.

## Excluded sessions, hand-checked

All five are unrelated to the PR they were excluded from:

- **01a07738** (hackerrank#39311, overlap 0.0): a different feature on branch `custom-prompt-in-questions`, from Sep 6. It edited a file outside the PR.
- **01a084fe** (hackerrank#39418, 0.09): on branch `model-config-api`, the head branch of #39311, addressing #39311's review comments. It's kept for #39311.
- **01a087e0** (hackerrank#39418, 0.12): model-selection work on branch `custom-prompt-in-questions`. Borderline, since it touched a nearby enricher and its spec, but it's a different branch and feature.
- **019fd74f** (workspace-service#757, 0.06): on branch `meavan-central-repo`, working on PR #756.
- **019fda3d** (workspace-service#757, 0.0): one turn on branch `fix-maven-regex` at the end of the window. A follow-up change, not this PR.

## Hook (M7)

- A dry run from a live session confirmed the hook's wiring.
- A real launch from hand-written stdin for pair-be#56 built the brief in 41.8 s for $0.18, with no denials. No real PR was created.

## Known issues

- **Status line before the URL.** Even with lean settings, the main agent adds a line like "All sections saved successfully." before the URL in most runs; naming that line as a bad example didn't help. The hook path hides this, because its `systemMessage` carries the URL.
- **Orchestration overhead and variance.** See the timings above. The main agent is Sonnet at low effort; the writers' latency is the larger, noisier part.
- **Session limit.** Everything runs on the Claude Code login, so the brief and the reviewer share that account's session limit. When a writer hits it, the main agent hits it too, so the `--empty` fallback never runs and the brief stays pending. The page waits, and `/pair` rebuilds the brief once it has been pending for 15 minutes.
- **Hints are broad on big PRs.** They cover 10 of 34 candidates on awt#1 but 43 of 60 on hackerrank#39311, where they help less.
- **Not rerun with the final code:** ideverse#763, hackerrank#39418, workspace-service#757, and pair-be#56 (the session limit). Their numbers above come from the default run with the earlier pushback prompt.

## Frontend

The Next.js page in `web/` is served from `web/out` by `pair-brief serve`, which builds it first when the build is missing or stale. Browser checks ran in headless Chrome. Checks that needed Claude stopped when the account hit its session limit: a headless `/pair` on ideverse#763 exited after 0.6 s with the limit message. That blocked the end-to-end `/pair` runs, the brief rebuild, the reviewer chat, and drafts written by the reviewer.

**Build and load:**

- `bun run build` (Next 16, Turbopack, static export) takes 4 to 8 s. `serve` skips it when `web/out` is newer than every source file.
- awt#1 (3,534 diff lines, 23 files): the PR title appears 0.34 to 0.6 s after navigation, and the first diff lines 0.37 to 0.66 s after.
- A full wheel scroll of awt#1's diff (about 68,500 px) had a 95th-percentile frame time of 33.4 ms, which is headless Chrome's fixed frame cadence, and one 50 ms long task.

**Checks run:**

| Check | Result |
|---|---|
| Split and unified selections on awt#1 give the right path, side, and range (6 cases) | pass |
| Kill calls in `internal/command/migrate.go` carry risky tags (lines 75, 82, 88, 96) | pass |
| Context expansion through the `file` endpoint | pass (22 to 27 lines in one gap) |
| awt#1 pass criteria visible on the page | 3 of 4; the current brief predates the pushback fix and has no layout reversal |
| Pending sections fill in without a reload (ideverse#763 copy, 5 sections saved 4 s apart with `pair-brief save`) | pass; each section appeared within one 2 s poll, 1 navigation, 0 console errors |
| Chat reconnects after a server restart | pass; reconnected about 6 s after the server returned, Send disabled while disconnected |
| Draft edit, approve, discard, restore, and state after reload (awt#1 copy, 3 synthetic drafts) | pass |
| Post gating: awt#1 shows only Post (disabled at 0 approved); ideverse#763 shows only Copy all | pass |
| Copy all writes each draft with a blob link at the head SHA | pass |
| Writing your own comment: + on a line, drag + over a range, or Comment on the selection chip (awt#1 copy) | pass; saved as approved, kept after reload |
| Close review removes the worktree (folder and `git worktree list`) | pass |
| Posting an approved draft on awt#1 returns a `review_url` | not run (needs a reviewer draft and the user's go-ahead) |
| Tool indicators, history after reload, and the next answer continuing the session | not run (session limit) |
| `/pair` end to end on awt#1 and one interviewstreet PR | not run (session limit) |

The draft and close checks ran against copies of the awt#1 and ideverse#763 briefs in a throwaway `PAIR_HOME`, with a second server on port 7789. The synthetic drafts never reached `~/.pair`.

**Tests:** 31 Vitest tests in 6 files (about 1.2 s), using hand-written fixtures. `uv run pytest -q` passes 61, including 8 new build-on-serve tests that put a fake `bun` on `PATH` and one for `POST /drafts`.

**Known issues:**

- **First build vs. `ensure_server`.** `prepare` waits 8 s for the server's health check. On a first run, `serve` installs and builds before it starts listening, which can take longer, so `prepare` can report the server as not running while it's still building. The page comes up once the build finishes.
- **A fresh page load reopens a closed review.** Closing ends the session and removes the worktree, but any later load of that review, including a leftover tab, starts a new session and adds the worktree back. This happened once during the checks.
- **Stale `supersedes` refs in older briefs.** The current awt#1 brief has 3 pushback items whose `supersedes` refs aren't decisions. The page now shows "reversed a decision" only when a ref names a decision in the same brief.
- **awt#1 verification lists `make test-tmux` twice:** as never run (an unchecked test-plan box), and inside a two-step command that passed in 6 runs after the last edit. The backend doesn't reconcile the two.
