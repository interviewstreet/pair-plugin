"use client";

import { type ReactNode, useState } from "react";

import { firstHunkRef } from "@/lib/refs";
import { useReview } from "@/lib/store";
import type { BriefItem } from "@/lib/types";

import { Markdown } from "./Markdown";
import { RefChip } from "./RefChip";
import { Tag as TagPill, type TagTone } from "./ui";

const STATUS_TAGS: Record<string, [string, TagTone]> = {
  failed: ["failed", "danger"],
  never_run: ["never run", "danger"],
  stale: ["stale", "caution"],
  passed: ["passed", "success"],
};

const KIND_TAGS: Record<string, [string, TagTone]> = {
  not_done: ["not done", "danger"],
  not_planned: ["not planned", "caution"],
  changed: ["changed", "info"],
};

const VISIBLE_CHIPS = 3;

/** How many items a section shows before "Show N more". */
const VISIBLE_ITEMS: Record<string, number> = { ask: 3, decisions: 6, verification: 6 };

const str = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

export function formatWhen(iso: unknown): string | null {
  const s = str(iso);
  if (!s) return null;
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** Markdown text, clamped to `lines` with a toggle when it's long. The CSS clamp must use the same line count. */
function Text({ text, lines = 3, className = "" }: { text: string; lines?: number; className?: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > lines * 64 || text.split("\n").length > lines;
  return (
    <div className={`text ${className}`}>
      <div className={long && !open ? "clamp" : undefined}>
        <Markdown text={text} tight />
      </div>
      {long && (
        <button
          type="button"
          className="more-toggle"
          onClick={(e) => {
            e.stopPropagation();
            setOpen(!open);
          }}
        >
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

function Chips({ refs }: { refs: string[] }) {
  const [all, setAll] = useState(false);
  if (!refs.length) return null;
  const hidden = refs.length - VISIBLE_CHIPS;
  const shown = all || hidden <= 0 ? refs : refs.slice(0, VISIBLE_CHIPS);
  return (
    <div className="chips">
      {shown.map((r) => (
        <RefChip key={r} value={r} />
      ))}
      {hidden > 0 && !all && (
        <button
          type="button"
          className="ref-chip more"
          onClick={(e) => {
            e.stopPropagation();
            setAll(true);
          }}
        >
          +{hidden}
        </button>
      )}
    </div>
  );
}

function Tag([label, tone]: [string, TagTone]) {
  return <TagPill tone={tone}>{label}</TagPill>;
}

function Item({
  item,
  tag,
  text,
  className = "",
  onClick,
  children,
}: {
  item: BriefItem;
  tag?: [string, TagTone];
  text?: string;
  className?: string;
  onClick?: () => void;
  children?: ReactNode;
}) {
  return (
    <li
      className={`brief-item ${onClick ? "clickable" : ""} ${className}`}
      onClick={onClick}
      onKeyDown={onClick ? (e) => e.key === "Enter" && onClick() : undefined}
      tabIndex={onClick ? 0 : undefined}
    >
      <div className="line">
        {tag && Tag(tag)}
        <Text text={text ?? str(item.text) ?? ""} lines={className === "summary" ? 6 : 3} />
      </div>
      {children}
      <Chips refs={strs(item.refs)} />
    </li>
  );
}

type Ctx = { scrollTo: (p: string, l?: number) => void; decisionRefs: ReadonlySet<string> };

function renderItem(section: string, item: BriefItem, i: number, { scrollTo, decisionRefs }: Ctx): ReactNode {
  switch (section) {
    case "summary":
      return <Item key={i} item={item} className="summary" />;
    case "look_first": {
      const hunk = firstHunkRef(strs(item.refs));
      return <Item key={i} item={item} onClick={hunk ? () => scrollTo(hunk.path, hunk.line) : undefined} />;
    }
    case "ask": {
      const when = formatWhen(item.at);
      const session = str(item.session);
      return (
        <Item key={i} item={item}>
          {(session || when) && (
            <div className="sub">
              {session && <span className="mono">{session.length > 24 ? `${session.slice(0, 24)}…` : session}</span>}
              {session && when && " · "}
              {when}
            </div>
          )}
        </Item>
      );
    }
    case "plan_vs_result":
      return <Item key={i} item={item} tag={KIND_TAGS[str(item.kind) ?? ""]} />;
    case "decisions": {
      const superseded = item.superseded === true;
      const by = str(item.superseded_by);
      const question = str(item.question);
      const answer = str(item.answer);
      const options = strs(item.options);
      return (
        <Item
          key={i}
          item={item}
          text={question ?? undefined}
          tag={superseded ? ["reversed", "caution"] : undefined}
          className={superseded ? "superseded" : ""}
        >
          {question && answer && <div className="sub answer">→ {answer}</div>}
          {options.length > 0 && <div className="sub">options: {options.join(" / ")}</div>}
          {superseded && by && (
            <div className="sub">
              reversed by <RefChip value={by} />
            </div>
          )}
        </Item>
      );
    }
    case "verification": {
      const runs = num(item.runs);
      const last = formatWhen(item.last_run);
      return (
        <Item key={i} item={item} tag={STATUS_TAGS[str(item.status) ?? ""]}>
          {(runs !== null || last) && (
            <div className="sub">
              {runs !== null && `${runs} run${runs === 1 ? "" : "s"}`}
              {last && ` · last ${last}`}
            </div>
          )}
        </Item>
      );
    }
    case "pushback": {
      // Briefs saved before the backend checked `supersedes` can name refs that aren't decisions.
      const reverses = strs(item.supersedes).some((r) => decisionRefs.has(r));
      return <Item key={i} item={item} tag={reverses ? ["reversed a decision", "caution"] : undefined} />;
    }
    case "churn": {
      const edits = num(item.edits);
      const turns = num(item.turns);
      const path = str(item.path);
      return (
        <Item key={i} item={item}>
          {(edits !== null || path) && (
            <div className="sub">
              {edits !== null && `${edits} edits`}
              {turns !== null && ` · ${turns} turns`} {path && <RefChip value={`file:${path}`} />}
            </div>
          )}
        </Item>
      );
    }
    default:
      return <Item key={i} item={item} />;
  }
}

const NO_REFS: ReadonlySet<string> = new Set();

/** The items shown before "Show N more". Reversed decisions are kept first, then shown in their original order. */
function visibleItems(name: string, items: BriefItem[]): BriefItem[] {
  const limit = VISIBLE_ITEMS[name];
  if (!limit || items.length <= limit) return items;
  if (name !== "decisions") return items.slice(0, limit);
  const reversed = (it: BriefItem) => Number(it?.superseded === true);
  const keep = new Set([...items].sort((a, b) => reversed(b) - reversed(a)).slice(0, limit));
  return items.filter((it) => keep.has(it));
}

export function BriefSectionItems({
  name,
  items,
  decisionRefs = NO_REFS,
}: {
  name: string;
  items: BriefItem[];
  decisionRefs?: ReadonlySet<string>;
}) {
  const scrollTo = useReview((s) => s.scrollTo);
  const [all, setAll] = useState(false);
  const ctx: Ctx = { scrollTo: (path, line) => scrollTo(path, line, "additions"), decisionRefs };
  if (!items.length) return <p className="muted">None.</p>;
  const capped = visibleItems(name, items);
  const hidden = items.length - capped.length;
  const shown = new Set(all ? items : capped);
  return (
    <>
      <ul className="brief-items">
        {items.map((item, i) => (shown.has(item) && item && typeof item === "object" ? renderItem(name, item, i, ctx) : null))}
      </ul>
      {hidden > 0 && (
        <button type="button" className="more-toggle" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show ${hidden} more`}
        </button>
      )}
    </>
  );
}

export const SECTION_TITLES: Record<string, string> = {
  summary: "Summary",
  look_first: "Look first",
  ask: "What was asked",
  plan_vs_result: "Plan vs result",
  decisions: "Decisions",
  verification: "Verification",
  pushback: "Pushback",
  side_effects: "Side effects",
  churn: "Churn",
  loose_ends: "Loose ends",
};

export function sectionTitle(name: string): string {
  return SECTION_TITLES[name] ?? name.replace(/_/g, " ");
}
