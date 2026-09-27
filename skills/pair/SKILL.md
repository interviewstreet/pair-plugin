---
name: pair
description: Build a review brief for a GitHub PR from the Claude and Codex sessions that built it, and open a local review page with a pair agent.
disable-model-invocation: true
argument-hint: owner/repo#N
effort: low
model: sonnet
allowed-tools: Bash(pair-brief *) Read(~/.pair/briefs/**) Edit(~/.pair/briefs/**) Write(~/.pair/briefs/**)
---

Build the review brief for `$ARGUMENTS`. Follow these steps exactly. Don't read transcripts, the diff, or the brief yourself, and don't write any brief text: the subagents write it from the evidence files.

Your final message is shown to the user as the result, so it must be the review page URL and nothing else: no status line, no summary of what the subagents did. Their replies are for you only. Don't run any command other than `pair-brief`, and don't check the output files yourself.

1. Run `pair-brief prepare "$ARGUMENTS"` with Bash. It prints JSON.
   - If `status` is `error`, reply with the error and stop.
   - If `status` is `ready` or `in_progress`, reply with the `url` value alone and stop.
2. `write` lists the sections to write, and `agents` names the subagent for each. In ONE message, start the subagent for every section in `write` except `summary`, so they run in parallel. For each:
   - `subagent_type`: `pair:` followed by the agent's name, for example `pair:pair-side-effects`
   - `description`: the section name, for example `side_effects`
   - `run_in_background`: `false`
   - `prompt`: `Brief folder: <dir>. Input: <inputs[section]>. Output: <outputs[section]>.` with the values from step 1.
3. After they all return, check each one's reply. A reply that says how many items were kept, even 0, means the section is saved. Only for a subagent that replied `not saved`, `denied:`, or failed, run `pair-brief save <dir> <section> --empty` so the page stops waiting for it.
4. Start `pair:pair-summary` the same way, with `description` `summary`, `run_in_background: false`, and the prompt `Brief folder: <dir>. Input: <inputs.summary>. Outputs: <outputs.summary> and <outputs.look_first>.`
5. If the summary subagent replied `not saved` or `denied:` for a section, or failed, run `pair-brief save <dir> <section> --empty` for each section it missed.
6. Reply with the `url` value alone. The whole reply looks like this, with no other lines:

   http://127.0.0.1:7788/review/?key=owner__repo__123
