"use client";

import { useContext, useMemo, useState, type ReactNode, type Ref } from "react";
import type { ExcerptMessage } from "@/lib/types";
import { shortPath, toTranscript, type DiffLine, type Entry } from "@/lib/transcript";
import { Markdown, PathsContext } from "./Markdown";
import { Square, User } from "./icons";

export const AGENT_NAMES: Record<string, string> = { claude: "Claude Code", codex: "Codex" };

const PATH_TOOLS = new Set(["Read", "Write", "Edit", "MultiEdit", "NotebookEdit"]);
const OUTPUT_LINES = 3;
const DIFF_LINES = 8;
const PROMPT_LINES = 8;

export function AgentMark({ agent }: { agent: string }) {
  return <span className={`tx-avatar ${agent === "codex" ? "codex" : "claude"}`}>{agent === "codex" ? ">_" : "✻"}</span>;
}

/** An excerpt as the agent's terminal showed it: the human's prompts in a box, the agent's replies and tool calls under it. */
export function Transcript({
  agent,
  messages,
  skipped = 0,
  moreAfter = 0,
  focusRef,
}: {
  agent: string;
  messages: ExcerptMessage[];
  skipped?: number;
  moreAfter?: number;
  focusRef?: Ref<HTMLDivElement>;
}) {
  const blocks = useMemo(() => toTranscript(messages, skipped, moreAfter), [messages, skipped, moreAfter]);
  const style = agent === "codex" ? "codex" : "claude";
  let focusGiven = false;
  const focusProps = (e: Entry) => {
    if (!e.focus || focusGiven) return {};
    focusGiven = true;
    return { ref: focusRef };
  };

  return (
    <div className={`transcript ${style}`}>
      {blocks.map((b, i) => {
        if (b.speaker === "gap") {
          return (
            <div key={i} className="tx-gap">
              {b.count} {b.after ? "more" : "earlier"} message{b.count === 1 ? "" : "s"} in this turn not shown
            </div>
          );
        }
        if (b.speaker === "human") {
          return (
            <section key={i} className="tx-block human">
              <header className="tx-who">
                <span className="tx-avatar human">
                  <User size={11} />
                </span>
                Human
              </header>
              {b.entries.map((e, j) => (
                <Focusable key={j} entry={e} {...focusProps(e)}>
                  <HumanEntry entry={e} style={style} />
                </Focusable>
              ))}
            </section>
          );
        }
        return (
          <section key={i} className="tx-block agent">
            <header className="tx-who">
              <AgentMark agent={agent} />
              {AGENT_NAMES[agent] ?? agent}
            </header>
            {b.entries.map((e, j) => (
              <Focusable key={j} entry={e} {...focusProps(e)}>
                <AgentEntry entry={e} style={style} />
              </Focusable>
            ))}
          </section>
        );
      })}
    </div>
  );
}

function Focusable({ entry, children, ref }: { entry: Entry; children: ReactNode; ref?: Ref<HTMLDivElement> }) {
  return (
    <div ref={ref} className={entry.focus ? "tx-entry focus" : "tx-entry"}>
      {entry.focus && <span className="tx-cited">Cited</span>}
      {children}
    </div>
  );
}

function HumanEntry({ entry, style }: { entry: Entry; style: string }) {
  if (entry.kind === "interrupt") {
    return (
      <div className="tx-interrupt">
        <Square size={10} /> Interrupted the agent
      </div>
    );
  }
  if (entry.kind === "answer") {
    return (
      <div className="tx-prompt answer">
        <span className="tx-caret">↳</span>
        <span>{entry.text ? <>Answered: {entry.text}</> : <span className="tx-subtle">Didn&apos;t answer</span>}</span>
      </div>
    );
  }
  if (entry.kind !== "prompt") return null;
  return (
    <div className="tx-prompt">
      <span className="tx-caret">{style === "codex" ? "›" : ">"}</span>
      <Clamped text={entry.text} lines={PROMPT_LINES} />
    </div>
  );
}

function Clamped({ text, lines }: { text: string; lines: number }) {
  const [open, setOpen] = useState(false);
  const long = text.split("\n").length > lines || text.length > lines * 90;
  return (
    <div className="tx-prompt-text">
      <div className={long && !open ? "tx-clamp" : undefined} style={{ WebkitLineClamp: lines }}>
        {text}
      </div>
      {long && (
        <button type="button" className="tx-more" onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show all"}
        </button>
      )}
    </div>
  );
}

function AgentEntry({ entry, style }: { entry: Entry; style: string }) {
  if (entry.kind === "text") {
    return (
      <div className="tx-line">
        <span className="tx-dot" />
        <div className="tx-body">
          <Markdown text={entry.text} tight />
        </div>
      </div>
    );
  }
  if (entry.kind === "question") {
    return (
      <div className="tx-line">
        <span className="tx-dot" />
        <div className="tx-body">
          <span className="tx-tool-name">Asked the human</span>
          <div className="tx-question">{entry.text}</div>
        </div>
      </div>
    );
  }
  if (entry.kind !== "tool") return null;
  return <ToolCall entry={entry} style={style} />;
}

function toolLabel(name: string, style: string): string {
  if (style === "codex") return name === "Bash" ? "Ran" : name === "Edit" ? "Edited" : name;
  return name === "Edit" || name === "MultiEdit" ? "Update" : name;
}

function ToolCall({ entry, style }: { entry: Extract<Entry, { kind: "tool" }>; style: string }) {
  const paths = useContext(PathsContext);
  const [open, setOpen] = useState(false);
  const arg = PATH_TOOLS.has(entry.name) ? shortPath(entry.arg, paths) : entry.arg.trim();
  const out = (entry.output ?? "").replace(/\s+$/, "");
  const outLines = out ? out.split("\n") : [];
  const diff = entry.diff ?? [];
  const hiddenOut = open ? 0 : Math.max(outLines.length - OUTPUT_LINES, 0);
  const hiddenDiff = open ? 0 : Math.max(diff.length - DIFF_LINES, 0);
  const longArg = arg.split("\n").length > 2 || arg.length > 180;
  const canToggle = longArg || outLines.length > OUTPUT_LINES || diff.length > DIFF_LINES;
  const label = toolLabel(entry.name, style);

  return (
    <div className="tx-line tool">
      <span className="tx-dot" />
      <div className="tx-body">
        {entry.name && (
          <div className={`tx-call ${longArg && !open ? "tx-clamp" : ""}`} style={{ WebkitLineClamp: 2 }}>
            <span className="tx-tool-name">{label}</span>
            {arg &&
              (style === "codex" ? (
                <span className="tx-arg"> {arg}</span>
              ) : (
                <span className="tx-arg">({arg})</span>
              ))}
          </div>
        )}
        {(outLines.length > 0 || diff.length > 0 || entry.output === "") && (
          <div className="tx-result">
            <span className="tx-elbow" aria-hidden />
            <div className="tx-result-body">
              {diff.length > 0 && <Diff lines={hiddenDiff ? diff.slice(0, DIFF_LINES) : diff} />}
              {outLines.length > 0 && (
                <pre className="tx-output">{(hiddenOut ? outLines.slice(0, OUTPUT_LINES) : outLines).join("\n")}</pre>
              )}
              {entry.output === "" && !diff.length && <span className="tx-subtle">(no output)</span>}
            </div>
          </div>
        )}
        {canToggle && (
          <button type="button" className="tx-more" onClick={() => setOpen(!open)}>
            {open ? "Show less" : hiddenOut + hiddenDiff > 0 ? `… +${hiddenOut + hiddenDiff} lines` : "Show all"}
          </button>
        )}
      </div>
    </div>
  );
}

function Diff({ lines }: { lines: DiffLine[] }) {
  return (
    <pre className="tx-diff">
      {lines.map((l, i) => (
        <div key={i} className={l.sign === "+" ? "add" : l.sign === "-" ? "del" : l.sign === "@" ? "hunk" : undefined}>
          <span className="sign">{l.sign === "@" ? "" : l.sign}</span>
          {l.text}
        </div>
      ))}
    </pre>
  );
}
