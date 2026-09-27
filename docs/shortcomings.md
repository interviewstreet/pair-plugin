# Shortcomings (Sep 27, 2026)

What the backend gets wrong or can't do yet, found by reading the code and checking the data on this machine. Each item says what to change, assuming the target setup below.

## Target setup

- **Builder machine.** `/pair` runs one command, `pair-brief submit <pr>`. It fetches the PR, matches sessions, uploads the whole session files of matched and borderline sessions plus the match result to S3, and prints the review URL. Nothing contacts this machine again.
- **Service.** Pulls PR data and the diff from GitHub at the pinned SHAs, parses the transcripts, extracts facts, runs the brief writers on our own LLM server, and publishes a bundle to S3.
- **VM.** Pulls the bundle, clones the repo and checks out the pinned SHAs, shows the diff view, and runs the pair agents in the pi harness. It holds drafts and posts reviews with the reviewer's GitHub login.
- **Upload.** The builder asks the service for a job and gets back a job ID, the review URL, and a presigned S3 upload URL. The upload (or an S3 event) starts the job. The payload stays in S3, so a failed job can rerun without the builder.
- **Bundle** (per repo, PR, and head SHA): `manifest.json` (status of each section, SHAs, versions, timings), `diff.patch` (GitHub's diff), `brief.json` and `brief.md`, `sessions/<agent>-<id>.md` (one readable transcript per session, each message headed by its ref), `sessions/index.json`, `evidence.json`, an `AGENTS.md` that explains the files and refs, and the raw transcripts under a separate prefix.

## Runs on the user's Claude Code

The first three items here are **fixed** in PoC2 and are kept as a record of what changed.

- ~~**The `/pair` skill runs a Sonnet agent just to start subagents.**~~ Fixed: the skill makes one `pair-brief submit` call and replies with the key and status (`skills/pair/SKILL.md`). It no longer starts subagents, reads transcripts, or waits on the brief.
- ~~**Writers are Claude Code subagents that save through a Write hook**~~ (`hooks/save_on_write.sh`, `cmd_hook_save`), because Claude Code's Bash checks block piping JSON. Fixed: the hook and the `hook-save` subcommand are gone. The service runs each writer with `claude -p --tools ""` so the reply is the JSON list itself, validates it in code through `pair.brief.save`, and retries once with the error as a hint (`src/pair/service/claude.py`, `src/pair/service/jobs.py`). Still to do: a direct LLM API call with schema-enforced output, instead of the CLI on a logged-in profile.
- ~~**The PR-created hook starts `claude -p "/pair …"`**~~ Fixed: `hooks/hook_pr_created.py` now execs `pair-brief submit <owner/repo>#<n>` directly, as an argv list, ten seconds after `gh pr create` returns.
- **The chat runs the Claude Code CLI through `claude_agent_sdk`**, and quietly uses the user's Claude Code login when no API key is set (`pair_agent.py`). Fix: the chat moves to the pi agents on the VM.
- **The service still runs the writers on a Claude Code login**, now on its own host rather than the user's laptop (`SubprocessRunner`, `default_login_check`, `/health`). The session limit moved; it didn't go away.

## Reaches back to the builder machine

- **Chat search and excerpts re-read session files from local paths** (`transcripts.py`). Fix: `sessions/*.md` in the bundle, which agents grep.
- **File text, context expansion, and the chat's Read, Grep, and Glob use the builder's clone** (`reviews.py`: `file_text`, `ensure_worktree`, `find_clone`). Fix: the VM's checkout.
- **PR comments and posting use the builder's `gh` accounts** (`fetch_comments`, `Review.post`, `github_accounts`). Fix: the VM, with the reviewer's login.
- **Saving, locks, the 15-minute stale timer, and the local server live on the builder** (`brief.py`, `server.py`). Fix: move them to the service, or drop them.

## Finding the right sessions

- **Repo detection depends on local clones** (`repos.py`). It walks up to `.git`, reads only `origin`, and falls back to regexes for `~/awt` and `~/orca`. It misses deleted worktrees elsewhere, forks (origin is the fork, the PR is upstream), and other clone folders. Fix: use the session's own data first (Codex `session_meta.git.repository_url` is recorded but unused; Claude records the cwd), check all remotes, and fall back to matching edit paths against the PR's files by path suffix.
- **Strong exact signals are unused.** Fix: keep a session when a PR commit SHA appears in its `git commit`, `git push`, or `git log` output, or when its `git push` output names the head branch.
- **Sessions with no measurable lines are always kept.** `keep` in `match.py` keeps `overlap=None`: `sed` edits, file-history records, and edits whose lines are all under 12 characters. Unrelated sessions that touched common files slip in. Fix: keep them only when another signal agrees.
- **Overlap uses exact line matches.** Formatters and later edits make real sessions look unrelated, and boilerplate lines (`return nil, err`, imports) inflate the score. Fix: normalize whitespace, ignore lines that are common in the base file, and measure in both directions.
- **The time window is fragile.** It runs from the earlier of the merge base and 24 h before the first commit, to 1 h after the last commit. After a rebase, planning more than 24 h before the first commit is lost, and an old cherry-picked commit makes the window huge. Fix: widen it with branch and commit signals, and cap its length.
- **Kept sessions lose their turns before the window**, so the original ask or plan can be cut off (`context` in `match.py`). Fix: extend back to the session start or the last branch switch.
- **Matching can't be redone later**, since the builder is never contacted again. Fix: send borderline ("related") sessions too, with their scores and rules, and let the service decide.
- **One PR built on several machines or by several people isn't supported.** Fix: accept several payloads per PR and merge them by session ID.

## Reading transcripts

- **One bad record hides a whole session.** `_safe_read` returns `None` on any error (`readers/__init__.py`). Fix: skip the record, count the skips, and report them.
- **Codex plans are never read.** `update_plan` calls (in 29 Codex files here) sit in `response_item` records, and `readers/codex.py` reads only `event_msg` records.
- **Claude task lists are ignored** (`TaskCreate`, `TaskUpdate`). Only `ExitPlanMode` and writes to `*plan*.md` count as plans.
- **Plans written as text aren't treated as plans:** design docs in the prompt, "here's the plan" in agent messages, and the PR description.
- **The Codex branch comes from the session start only** (`codex.py`), so a later `git checkout -b` isn't seen. Fix: track branch switches from commands.
- **Relative Codex edit paths are dropped**, because `locate` needs an absolute path. Fix: resolve them against the session cwd.
- **Messages are clipped** to 2,000 characters and tool output to 4,000. Fix: the service keeps the full text in `sessions/*.md`.
- **Resumed or forked sessions may double count** churn and pushback. Fix: dedupe turns by message ID across files.

## Brief content

- **`plan_vs_result` assumes a plan.** 5 of the 7 briefs here had no detected plan, yet it ran on all 7. Fix: when there's no plan, compare against all the human's prompts plus the PR description, drop or narrow `not_planned`, write nothing when the ask is too thin, and label each item with what it was compared against (plan, ask, or PR description).
- **`plan_vs_result` reports process, not code.** hackerrank#39418 listed review threads left unresolved, and its "RSpec not run" item repeats `verification`. Fix: report code departures only, and leave testing to `verification`.
- **`plan_vs_result` never sees the diff**, only file names, yet it may cite `hunk:` refs. Fix: send the relevant added lines.
- **`side_effects` sees only added lines**, with no surrounding lines and no test-file flag, and the regexes in `risky.py` are noisy (`fetch(`, any backtick string). Fix: add context lines and path flags.
- **`verification` ignores CI**, and it can list the same test twice: as an unchecked box and as a passing run (awt#1). Fix: add `gh pr checks`, and reconcile checkboxes with runs.
- **`look_first` changes between runs** of the same evidence (hackerrank#39311). Fix: rank candidates in code (risky hunks, stale tests, churn, reversed decisions), have the model order and explain them, and use temperature 0.
- **Pushback hints are broad on big PRs**: 43 of 60 candidates on hackerrank#39311.
- **Refs are long** (`claude:<uuid>:<uuid>`), which costs tokens and leads to dropped refs. Fix: short aliases (`R1`, `R2`), mapped back in code.
- **Nothing records how a section was made.** Fix: save the model, prompt version, and input hash with each section.

## Speed

- **Current times:** `prepare` takes 2 to 3.6 s, and the LLM step 30 to 90 s. The same writer took 20 s and 39 s on identical input.
- **No section has a timeout**, so a stuck section waits 15 minutes. Fix: hard timeouts (30 to 40 s), then save it empty and mark it timed out.
- **The summary waits for the slowest writer.** Fix: start it from the facts, or after two of the three writers finish, or give it a faster model.
- **Every section uses the same model.** Fix: a small model for `side_effects` and a stronger one for pushback and plan checks, with the shared PR context first so prompt caching works.
- **Every run rescans all session files** (about 700 files and 1 GB here) and re-parses every file changed since the window start. Fix: a local SQLite index of session metadata that reads only new lines.
- **The chat parses the same sessions again.** Fix: the service publishes `sessions/*.md` right after parsing, before the writers finish.
- **Every push redoes the whole brief.** Fix: cache sections by input hash, and rerun only the ones that changed.

## GitHub data

- **File lists are inconsistent.** `match` and `prepare` merge `gh`'s file list with the parsed diff, but `overlap`, `churn`, and `verification` use only `pr.files`, which can be truncated on big PRs. Fix: one merged list everywhere.
- **Large diffs fail.** GitHub refuses the whole diff with a 406 when it's too large. Fix: up to 300 files, use the compare endpoint at the pinned SHAs; up to 3,000, use `/pulls/{n}/files` after checking the head SHA hasn't moved; above that, the builder sends the diff. When a file's `patch` is missing (binary or very large files), diff the two versions of the file on the service.
- **Everything must use the pinned SHAs**, or `hunk:` refs and matches won't line up after a new push.
- **The service needs its own GitHub access**, such as a GitHub App or bot token set as `GH_TOKEN`.
- **Only github.com works** (`normalize_remote`, `parse_pr_spec`).
- **Commit lists may be capped.** GitHub's REST API lists at most 250 commits per PR, and the window starts from the earliest commit. Check what `gh pr view --json commits` returns on long PRs.

## Reliability and operations

- ~~**Job state is a pending list plus a 15-minute timer.**~~ Partly fixed: `reviews/<key>/manifest.json` carries `queued`, `running`, `done`, or `failed` per section with an `updated_at` and an `error` (`src/pair/service/manifest.py`). The builder's local brief dir still uses the `pending` list and the 15-minute timer. Still to do: `timed out` as its own status, and attempts, timings, tokens, and cost per section.
- **Two runs can start at once**, because `prepare` checks for an existing run outside the lock. Fix: a lease in the service, and dedupe by PR, head SHA, and payload hash.
- **The hook only sees `gh pr create` inside Claude Code.** PRs from Codex, the GitHub UI, or a push followed by the web form are missed. Fix: add a Codex hook, poll open PRs, or build the brief on first open.
- **Briefs go stale after new pushes**, and the reviewer can't trigger a new gather. Fix: resubmit on each push, and show "brief is for SHA X, PR is now at Y".
- **Personal defaults are hardcoded:** `allowed_repos=["five-sh/awt"]`, GitHub account names, `~/dev`, and the awt and orca layouts (`config.py`, `repos.py`). Fix: config plus auto-detection.
- **Nothing is cleaned up.** Worktrees are removed only on Close (6 are left here), and old briefs pile up per SHA. Fix: time-based cleanup and S3 lifecycle rules.
- **A failed upload is lost.** Fix: queue it on the builder and retry.
- **Payloads have no schema version or diagnostics.** Fix: a versioned schema, and include the plugin and reader versions, window, files scanned, skipped records, match rules, and timings.
- **There's little visibility.** Fix: structured logs, tokens and cost per call, error counts, and retries with backoff for the LLM server and GitHub.

## Security (deferred)

- **Secrets stay in the transcripts for now**, so they reach the service and S3 as they are. Keep the bucket private, give read access only to reviewers who can see the repo, and leave a spot in the upload step for redaction later.
- **Agents on the VM can post or push on their own.** Fix: block `gh pr review`, `gh api -X POST`, and `git push` from agent shells, and post only through the approve-then-post step.

## Tests and evals

- **Matching is checked only against the live session stores** (`EXPECTED` in `tests/test_match.py`, marked `local`). Fix: redacted fixture copies so it runs in CI, and precision and recall tracked on every change.
- **LLM output variance isn't measured.** Fix: run each section several times on fixed evidence, and track how much the results agree.

## Measured on this machine

- Session stores: 429 Claude files (534 MB) and 273 Codex files (489 MB).
- Whole session files per brief, kept plus borderline: 0.3 to 44 MB raw, 0.1 to 7.4 MB gzipped. The largest is hackerrank#39311: 15 sessions in 21 files, 7.4 MB gzipped.
- Plans: 2 of the 7 briefs had a detected plan (awt#1 and hackerrank#39311).
