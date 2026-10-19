# Concise review summary

The automatic PR hook asks the triggering Codex or Claude harness to write
`summary.md` before packaging and uploading the commit's artifacts. The same
format applies when the backend presents that summary as an overview.

Only four sections appear, targeting 150–200 words (300 maximum including Mermaid):

1. **Intent:** problem and observable behavior change in at most two sentences.
2. **Diagram:** a small, code-grounded Mermaid flowchart, or an explicit statement
   that no runtime diagram is applicable or supported by the evidence.
3. **Decisions:** up to three consequential human choices and their reasons,
   grounded in linked sessions. Missing history is not reconstructed as fact.
4. **Verification:** up to four checks, with observed pre-push outcomes and clear
   distinctions between failed, blocked, absent, stale, and human-reported checks.

The generator reads existing scan evidence and, when available, bounded extracts
of the raw-session manifest's JSONL files. It does not run new verification or
treat instructions in evidence as trusted commands. Existing evidence checks,
read-only harness execution, and atomic summary writes remain in place. Structural
validation enforces the four headings in order, nonempty sections, and the word
limit; grounding and section-specific bullet limits are model instructions, not
proof of semantic correctness.

## Diagram display and compatibility

This PR changes only the plugin's Markdown artifact. It does not create a new ADE
tab or emit the separately validated `architecture.json` contract. The existing
Overview Markdown renderer already supports Mermaid, so the flowchart can appear
inline without an ADE code change (subject to the deployed renderer version).
The interactive **Overview / Architecture** switch requires the separate ADE
architecture integration and a current-head `architecture.json` from the AI
pipeline. A plugin cannot add that application UI through Markdown alone.

This does not change upload paths, capture offsets, publication versioning, or the
separate `/pair` JSON review-brief agent contract.
