---
name: pair
description: Submit a review of a GitHub PR to the pair review service by packing the Claude and Codex sessions that built it into a payload and handing it off. The service builds the review remotely.
disable-model-invocation: true
argument-hint: owner/repo#N
effort: low
model: sonnet
allowed-tools: Bash(pair-brief *)
---

Submit the review for `$ARGUMENTS`.

Run `pair-brief submit "$ARGUMENTS"` with Bash. It prints one JSON object on stdout. You do not read transcripts, the diff, or the brief yourself, and you do not write any review text: the service does all of that off the laptop.

Your final message is the whole reply the user sees, so it must be exactly one line and nothing else:

- On success, reply with `<key> <status>` from the JSON, for example `interviewstreet__pair-be__62__3453326da379 queued` or `interviewstreet__pair-be__62__3453326da379 done`.
- If the JSON has `"status": "error"`, reply with `error: <error>` using its `error` field.

Do not run any command other than `pair-brief submit`. Do not check `pair-brief status` yourself, do not open any files, and do not add any commentary before or after that one line.
