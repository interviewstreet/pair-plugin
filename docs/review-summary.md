# Concise review summary

The automatic PR hook asks the triggering Codex or Claude harness to write
`summary.md` before packaging and uploading the commit's artifacts. The same
format applies when the backend presents that summary as an overview.

The initial draft has four sections, targeting 150–200 words (300 maximum including Mermaid):

1. **Intent:** problem and observable behavior change in at most two sentences.
2. **Diagram:** a small, code-grounded Mermaid flowchart, or an explicit statement
   that no runtime diagram is applicable or supported by the evidence.
3. **Decisions:** up to three consequential human choices and their reasons,
   grounded in linked sessions. Missing history is not reconstructed as fact.
4. **Verification:** up to four checks, with observed pre-push outcomes and clear
   distinctions between failed, blocked, absent, stale, and human-reported checks.

When a valid interactive architecture artifact is generated, the final Markdown
omits the entire **Diagram** section: Overview shows only Intent, Decisions and
Verification, while ADE's Architecture tab shows the board. If generation is
unavailable or invalid, the inline diagram remains as a Markdown fallback.
This selection is plugin-side; it does not detect whether an older deployed ADE
supports the board, so deploy the ADE renderer before enabling this plugin version.

The generator reads existing scan evidence and, when available, bounded extracts
of the raw-session manifest's JSONL files. It does not run new verification or
treat instructions in evidence as trusted commands. Existing evidence checks,
read-only harness execution, and atomic summary writes remain in place. Structural
validation enforces the four headings in order, nonempty sections, and the word
limit; grounding and section-specific bullet limits are model instructions, not
proof of semantic correctness.

## Diagram display and compatibility

The existing Overview Markdown renderer supports inline Mermaid. For the draggable
board, the plugin also makes a second read-only call to the same Codex or Claude
harness to generate `generated_artifacts/architecture.json`. No separate model key
or AI-pipeline worker is needed. Each call has the existing five-minute timeout.

The JSON uses ADE's v1 artifact contract with diff-only citations and empty
`episode_ids` (the plugin does not generate episodes). Validation binds the head
to both the artifact directory and the snapshot's last PR commit, checks strict
fields, lengths, graph references and connected flows, and verifies citations lie
inside actual new-side PR diff hunks. Citations do not prove a model's semantic
interpretation is correct. Deletion-only or insufficient evidence can produce no
diagram rather than fabricated references.

The writer atomically saves valid output and removes an older diagram before a
retry. Null, invalid, oversized or timed-out diagram output is logged and does not
block the Markdown upload. Symlinked output paths are rejected. The existing ZIP
retains the versioned files and exposes a root `generated_artifacts/architecture.json`
alias alongside `overview.md` for ADE's extracted publisher bundle reader.

The interactive **Overview / Architecture** switch still requires deployment of
the separate ADE architecture integration. Older ADE renders the Markdown and
ignores the extra JSON. Pair BE must preserve the root generated-artifacts path
when extracting the existing ZIP. No backend or AI-pipeline API change is made
here; live S3 extraction is a rollout verification, not covered by unit tests.

This does not change upload paths, capture offsets, publication versioning, or the
separate `/pair` JSON review-brief agent contract.
