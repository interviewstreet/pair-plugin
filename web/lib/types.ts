export type Side = "additions" | "deletions";

export interface ReviewListItem {
  key: string;
  repo: string | null;
  number: number | null;
  title: string | null;
  head_sha: string;
  pending_count: number;
  updated_at: string | null;
}

export interface PrInfo {
  repo: string;
  number: number;
  title: string;
  url: string;
  head_branch: string;
  base_branch: string;
  body: string;
}

export interface SessionInfo {
  agent: string;
  id: string;
  role: string;
  opened_pr: boolean;
  title: string | null;
  started_at: string | null;
}

export interface FileStat {
  path: string;
  additions: number;
  deletions: number;
}

export interface ReviewMeta {
  pr: PrInfo;
  head_sha: string;
  can_post: boolean;
  post_account: string | null;
  sessions: SessionInfo[];
  files: FileStat[];
}

export const SECTION_ORDER = [
  "summary",
  "look_first",
  "ask",
  "plan_vs_result",
  "decisions",
  "verification",
  "pushback",
  "side_effects",
  "churn",
  "loose_ends",
] as const;

export type SectionName = (typeof SECTION_ORDER)[number];

/** Every item has text and refs; sections add their own fields, and unknown ones may appear. */
export interface BriefItem {
  text: string;
  refs: string[];
  [extra: string]: unknown;
}

export interface Brief {
  pr: PrInfo;
  head_sha: string;
  sessions: SessionInfo[];
  sections: Record<string, BriefItem[] | undefined>;
  pending: string[];
  created_at?: string;
  updated_at?: string;
}

export interface RiskyHunk {
  ref: string;
  path: string;
  line: number;
  tags: string[];
  added: string;
}

export interface ExcerptMessage {
  role: string;
  text: string;
  focus?: boolean;
}

export interface Excerpt {
  ref: string;
  turn?: string;
  agent: string | null;
  session_id: string | null;
  session_role: string | null;
  session_title?: string | null;
  timestamp: string | null;
  truncated?: boolean;
  /** Messages left out after the first one, and after the last one shown. */
  skipped?: number;
  more_after?: number;
  messages: ExcerptMessage[];
}

export type DraftStatus = "draft" | "approved" | "discarded" | "posted";

export interface Draft {
  id: string;
  path: string;
  side: Side;
  line: number;
  start_line?: number | null;
  body: string;
  status: DraftStatus;
  /** "reviewer" is the pair agent's stored name. Missing on drafts saved before comments could be written on the page. */
  author?: "reviewer" | "user";
  created_at: string;
  updated_at?: string;
  posted_at?: string;
  review_url?: string;
}

export interface NewDraft {
  path: string;
  side: Side;
  line: number;
  start_line: number | null;
  body: string;
}

/** The selection attached to a chat message, as the WebSocket expects it. */
export interface Selection {
  path: string;
  side: Side;
  start: number;
  end: number;
}

export type HistoryRecord =
  | { role: "user"; id: string; text: string; selection: Selection | null }
  | { role: "assistant"; id: string; text: string }
  | { role: "tool"; id: string; name: string; summary: string }
  | { role: "draft"; draft: Draft }
  | { role: "error"; text: string };

export type ServerEvent =
  | { type: "history"; messages: HistoryRecord[]; busy: boolean }
  | { type: "assistant_text"; message_id: string; text: string }
  | { type: "tool_use"; message_id: string; name: string; summary: string }
  | { type: "draft"; draft: Draft }
  | { type: "turn_done"; cost_usd: number | null; session_cost_usd?: number | null; duration_s?: number | null }
  | { type: "error"; message: string };

export type ClientEvent =
  | { type: "user_message"; text: string; selection: Selection | null }
  | { type: "interrupt" };

export interface PostResult {
  review_url: string;
  drafts: Draft[];
}

export interface CloseResult {
  closed: boolean;
  worktree_removed: boolean;
}
