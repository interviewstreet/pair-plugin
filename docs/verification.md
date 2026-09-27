# M0 verification (Sep 24, 2026)

Versions: Claude Code 2.1.280, `claude-agent-sdk` 0.2.158 (Python), `gh` 2.100.0, `uv` 0.12.18, Python 3.14.

The probes used a throwaway plugin with one skill, two subagents, a `bin/` script, and a `PostToolUse` hook, run headless on Haiku and Sonnet. Total model cost was under $0.20.

## 1. Headless skill runs and the subagent tool name

- `claude -p "/probe hello world" --plugin-dir <plugin>` runs the plugin skill in headless mode. The skill is registered as `probe:probe`, and the bare `/probe` resolves to it.
- The model calls subagents with the **`Agent`** tool. The init message still lists the tool as `Task` (the old name, kept as an alias).
- Plugin subagents are namespaced: `subagent_type` is `<plugin>:<agent>`, so ours are `pair:pair-side-effects` and so on.
- Subagents run in the background by default. The skill must ask for `run_in_background: false`. Two foreground `Agent` calls in one message ran in parallel and both returned before the parent continued.
- The skill's `allowed-tools` grant (`Bash(probe-bin *)`) also covered the subagents' Bash calls. With `--setting-sources project` there were no permission denials.
- Headless runs should still pass `--allowedTools`, and put the prompt first because the flag takes several values.

## 2. How the skill finds `bin/pair-brief`

- The plugin's `bin/` directory is on the Bash tool's `PATH` for the main agent and for subagents, so the skill and agents call `pair-brief` as a bare command.
- `CLAUDE_PLUGIN_ROOT` is **not** exported to the Bash tool's environment.
- `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_SKILL_DIR}` **are** substituted inside the skill body, and in `allowed-tools` rules.

## 3. `--plugin-dir` and hooks

- `claude --plugin-dir <path> plugin list` shows the plugin as `pair@inline`, loaded.
- `${CLAUDE_PLUGIN_ROOT}` expands inside `hooks.json`, and the hook process also gets `CLAUDE_PLUGIN_ROOT` in its environment.
- A `PostToolUse` hook on `Bash` receives `tool_input.command` and a `tool_response` object with `stdout`, `stderr`, `interrupted`, `isImage`, and `noOutputExpected`.
- The hook also fires for Bash calls made by subagents; those carry `agent_type`.

## 4. Agent SDK read-only enforcement

The configuration that held:

- `tools=["Read", "Grep", "Glob"]`: the base set of built-in tools. Anything not listed doesn't exist for the model.
- `allowed_tools=["mcp__pair__*"]`
- `disallowed_tools=["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task"]`
- `setting_sources=[]`, so user settings can't add allow rules, hooks, or plugins.
- **`strict_mcp_config=True`.** Without it, the claude.ai account connectors (`mcp__claude_ai_Claude_Docs__create`, `update`, `delete`, and so on) appeared in the tool list, which is a write path outside the worktree.
- `can_use_tool` allows the `mcp__pair__*` tools, allows Read, Grep, and Glob only when every path they name resolves inside the worktree, and denies everything else. The SDK warns that `allowed_tools` shadows the callback for `mcp__pair__*`; that's expected, and pytest filters the warning.

Read, Grep, and Glob are deliberately **not** in `allowed_tools`: an allow rule skips `can_use_tool` (that's what the shadowing warning says), so the path check would never run. Going through the callback, a live reviewer's Read of a probe file in the home directory was denied (`tests/test_reviewer_local.py`).

When asked to use Write, Bash, WebFetch, and Agent, the model reported that it had none of them, and no files were created. The custom MCP tool worked.

`extra_args={"no-session-persistence": None}` keeps reviewer sessions out of `~/.claude/projects`, and a `ClaudeSDKClient` still keeps multi-turn context in memory.

## 5. `gh` tokens per account

`gh auth token --user five-sh` and `gh auth token --user kousik-sh` both exit 0. `github.py` sets `GH_TOKEN` from the account configured for the repo's owner, for reads and for posting.

## 6. Claude interruption markers

Both appear as `text` blocks in `type: "user"` records whose `message.content` is a list:

- `[Request interrupted by user]` (94 occurrences on this machine)
- `[Request interrupted by user for tool use]` (39 occurrences)

The reader matches the prefix `[Request interrupted`.

## 7. Turning thinking off for the subagents

- The docs (sub-agents page) say subagents inherit the main conversation's thinking configuration, and there is no per-subagent thinking setting.
- Headless (the hook's `claude -p`): set `MAX_THINKING_TOKENS=0` in the environment. The probe confirmed that the variable reaches both the main session and subagents.
- Inside a normal interactive session (Opus, `xhigh`), subagent frontmatter can't turn thinking off. It can set `effort: low`, which overrides the session's effort level for that subagent. All four agents set `model: sonnet` and `effort: low`.
- Subagent thinking blocks don't appear in `stream-json` output, so thinking tokens per subagent weren't measured here. M8 records the timings.

## 8. Where the reviewer's credentials come from

`ANTHROPIC_API_KEY` isn't set. With no key, the Agent SDK runs its bundled Claude Code CLI with the Claude Code login (`apiKeySource: none` in the init message), and that worked.

Decision: the reviewer passes the key from the env var named by `anthropic_api_key_env` when that variable is set. Otherwise it uses the Claude Code login, so usage counts against that account's plan.

# Found while building (M4 to M8)

## 9. Subagents can't pass JSON through Bash, which is why the writers moved to the service

The plan had each writer subagent use Read and Bash, piping its JSON list into `pair-brief save`. In the first M5 run that took over 300 s, because Claude Code's Bash safety checks block every way of doing it:

- a heredoc containing JSON (`Contains brace with quote character (expansion obfuscation)`);
- a redirect to a file outside the session's working directories;
- a compound command that writes a file and then runs `pair-brief save`.

Asking for Write and Bash in one message didn't help either: Sonnet waits for the Write result before it sends the Bash call.

M4 to M8 worked around this with a `PostToolUse` hook on `Write` (`hooks/save_on_write.sh`, `pair-brief hook-save`) that caught each subagent's output file and saved the section. PoC2 removed the hook, the `out/<section>.json` files, and the `hook-save` subcommand along with it, because the writers no longer run inside the user's session at all. What ships now:

- `/pair` makes one call, `pair-brief submit <pr>` (`skills/pair/SKILL.md`, `src/pair/submit.py`). It packs `meta.json`, the brief directory, and the raw session JSONL into a `tar.gz` and POSTs it to `{service_url}/submit`. Nothing else runs on the builder: no local review server, no Claude call.
- The service runs each writer itself with `claude -p … --tools "" --output-format json` (`src/pair/service/claude.py`). With no tools there is no Write to catch, so the reply *is* the JSON list and the prompt says so.
- `src/pair/service/jobs.py` parses and validates that reply through the same `pair.brief.save`, and on a `SaveError` retries the call once with the error as a hint. The section's status and any final error land in `manifest.json` instead of in a hook's stderr.
- The summary writer produces `summary` and `look_first` in one call, so without a Write tool it replies with one object holding both lists.
- Taking Bash away in M5 also stopped a stray `find /` from the side-effects writer; the service keeps the tool list empty for the same reason.

## 10. Skill frontmatter and the Agent tool

- The skill's `model: sonnet` is honored: the main agent ran as Sonnet even when the session default was Opus.
- The skill's `allowed-tools` grants, including path rules like `Write(~/.pair/briefs/**)`, apply to the subagents it starts in headless runs. No denials across the M8 runs.
- The `Agent` tool call needs a `description` parameter. Without one it failed with "`description` … provided as `unknown`", so the skill names the section there.

## 11. Headless flags for the hook

- `--no-session-persistence` keeps the hook's own `/pair` session out of `~/.claude/projects`, so it never shows up as a builder session.
- `--setting-sources project --strict-mcp-config` skips user settings, `SessionStart` hooks, and MCP servers. The hook uses both. awt#1 took 90.7 s with default settings and 53 to 60 s in lean mode (five runs with the final code).
- The main agent still adds a status line ("All sections saved successfully.") before the URL in most runs, with either setting. In the hook path the user doesn't see that reply: the hook's `systemMessage` carries the URL.

## 12. Agent SDK cost is cumulative

`ResultMessage.total_cost_usd` is the running total for the client session, not the cost of the last turn. The reviewer sends the difference from the previous total as the turn's `cost_usd` and the running total as `session_cost_usd`.

## 13. Placeholder review page (deviation)

The plan says M6 has no HTML page. The server serves a small `placeholder.html` at `/review/` until the frontend's `web/out` build exists, so the brief, excerpts, drafts, and chat can be checked by hand. The static export replaces it once built.
