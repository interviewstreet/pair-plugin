import type { ExcerptMessage } from "./types";

export type DiffLine = { sign: "+" | "-" | " " | "@"; text: string };

export type Entry =
  | { kind: "prompt"; text: string; focus: boolean }
  | { kind: "answer"; text: string; focus: boolean }
  | { kind: "interrupt"; focus: boolean }
  | { kind: "text"; text: string; focus: boolean }
  | { kind: "question"; text: string; focus: boolean }
  | { kind: "tool"; name: string; arg: string; output: string | null; diff: DiffLine[] | null; focus: boolean };

export type Block = { speaker: "human" | "agent"; entries: Entry[] } | { speaker: "gap"; count: number; after: boolean };

const INTERRUPT = "[Request interrupted";
const FILE_TOOL = /^(Read|Write|MultiEdit|NotebookEdit) (.+)$/s;
const CLAUDE_EDIT = /^Edit (.+?): -(['"].*?['"]) \+(['"].*)$/s;
const CODEX_EDIT = /^Edit (.+?):\n(.*)$/s;
const SEARCH_TOOL = /^(Grep|Glob) (.*)$/s;
const ASKED = /^Asked: (.*)\nAnswer: (.*)$/s;
const NAMED_TOOL = /^([\w.-]+): (.*)$/s;

/** Undoes a Python repr of a string, as the Claude reader writes Edit arguments. Tolerates a cut-off closing quote. */
function unrepr(s: string): string {
  const q = s[0];
  let body = s.slice(1);
  if (body.endsWith(q)) body = body.slice(0, -1);
  const esc: Record<string, string> = { n: "\n", t: "\t", r: "", "\\": "\\", "'": "'", '"': '"' };
  return body.replace(/\\([ntr\\'"])/g, (_, c: string) => esc[c]);
}

function lines(sign: "+" | "-", text: string): DiffLine[] {
  return text ? text.split("\n").map((t) => ({ sign, text: t })) : [];
}

function unifiedDiff(text: string): DiffLine[] | null {
  if (!text.trim()) return null;
  return text
    .replace(/\n+$/, "")
    .split("\n")
    .filter((l) => !l.startsWith("+++") && !l.startsWith("---") && !l.startsWith("diff --git") && !l.startsWith("index "))
    .map((l) => {
      const c = l[0];
      if (c === "+" || c === "-") return { sign: c, text: l.slice(1) };
      if (c === "@") return { sign: "@", text: l };
      return { sign: " ", text: l.startsWith(" ") ? l.slice(1) : l };
    });
}

function tool(text: string, focus: boolean): Entry {
  const base = { output: null, diff: null, focus };
  if (text.startsWith("Bash: ")) {
    // Codex writes the command and its output as one message.
    const cut = text.indexOf("\n→ ");
    return cut < 0
      ? { kind: "tool", name: "Bash", arg: text.slice(6), ...base }
      : { kind: "tool", name: "Bash", arg: text.slice(6, cut), ...base, output: text.slice(cut + 3) };
  }
  let m = CLAUDE_EDIT.exec(text);
  if (m) return { kind: "tool", name: "Edit", arg: m[1], ...base, diff: [...lines("-", unrepr(m[2])), ...lines("+", unrepr(m[3]))] };
  m = CODEX_EDIT.exec(text);
  if (m) return { kind: "tool", name: "Edit", arg: m[1], ...base, diff: unifiedDiff(m[2]) };
  m = FILE_TOOL.exec(text) ?? SEARCH_TOOL.exec(text) ?? NAMED_TOOL.exec(text);
  if (m) return { kind: "tool", name: m[1], arg: m[2], ...base };
  return { kind: "tool", name: "", arg: text, ...base };
}

/** Groups excerpt messages by who said them: the human's prompts and answers, and the agent's text and tool calls. */
export function toTranscript(messages: ExcerptMessage[], skipped = 0, moreAfter = 0): Block[] {
  const blocks: Block[] = [];
  const push = (speaker: "human" | "agent", entry: Entry) => {
    const last = blocks.at(-1);
    if (last && last.speaker === speaker) last.entries.push(entry);
    else blocks.push({ speaker, entries: [entry] });
  };
  const lastAgentEntry = () => {
    const last = blocks.at(-1);
    return last?.speaker === "agent" ? last.entries.at(-1) : undefined;
  };

  messages.forEach((m, i) => {
    const focus = m.focus === true;
    if (m.role === "user") {
      push("human", m.text.startsWith(INTERRUPT) ? { kind: "interrupt", focus } : { kind: "prompt", text: m.text, focus });
    } else if (m.role === "assistant") {
      push("agent", { kind: "text", text: m.text, focus });
    } else if (m.text.startsWith("→ ")) {
      const prev = lastAgentEntry();
      if (prev?.kind === "tool" && prev.output === null && !prev.diff) {
        prev.output = m.text.slice(2);
        prev.focus ||= focus;
      } else push("agent", { kind: "tool", name: "", arg: "", output: m.text.slice(2), diff: null, focus });
    } else {
      const asked = ASKED.exec(m.text);
      if (asked) {
        const prev = lastAgentEntry();
        const blocksBefore = blocks.at(-1);
        // The AskUserQuestion call's JSON says the same thing as the question.
        if (prev?.kind === "tool" && prev.name === "AskUserQuestion" && blocksBefore?.speaker === "agent") {
          blocksBefore.entries.pop();
        }
        push("agent", { kind: "question", text: asked[1], focus });
        push("human", { kind: "answer", text: asked[2] === "None" ? "" : asked[2], focus });
      } else push("agent", tool(m.text, focus));
    }
    if (i === 0 && skipped > 0) blocks.push({ speaker: "gap", count: skipped, after: false });
  });
  if (moreAfter > 0) blocks.push({ speaker: "gap", count: moreAfter, after: true });
  return blocks;
}

/** A path as the PR names it when it ends with one of the PR's files, else with the home folder shortened. */
export function shortPath(path: string, prPaths: readonly string[]): string {
  const p = path.trim();
  if (!p.startsWith("/")) return p;
  const hit = prPaths.find((f) => p.endsWith(`/${f}`));
  if (hit) return hit;
  return p.replace(/^\/Users\/[^/]+\//, "~/").replace(/^\/home\/[^/]+\//, "~/");
}
