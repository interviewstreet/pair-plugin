import type { Draft, HistoryRecord, Selection, ServerEvent } from "./types";

export type ChatItem =
  | { kind: "user"; id: string; text: string; selection: Selection | null }
  | { kind: "assistant"; id: string; text: string }
  | { kind: "tool"; id: string; name: string; summary: string }
  | { kind: "draft"; id: string; draft: Draft }
  | { kind: "error"; id: string; text: string };

export type ConnectionStatus = "connecting" | "open" | "reconnecting" | "closed" | "refused";

export interface TurnStats {
  cost_usd: number | null;
  session_cost_usd: number | null;
  duration_s: number | null;
}

export interface ChatState {
  items: ChatItem[];
  busy: boolean;
  status: ConnectionStatus;
  lastTurn: TurnStats | null;
  seq: number;
}

export type ChatAction =
  | { type: "server"; event: ServerEvent }
  | { type: "sent"; text: string; selection: Selection | null }
  | { type: "status"; status: ConnectionStatus }
  | { type: "reset" };

export const initialChat: ChatState = { items: [], busy: false, status: "connecting", lastTurn: null, seq: 0 };

/** The server sends this when a message arrives mid-turn; the turn is still running. */
const STILL_BUSY = "the pair agent is still answering";

function fromHistory(records: HistoryRecord[]): ChatItem[] {
  return records.map((r, i): ChatItem => {
    switch (r.role) {
      case "user":
        return { kind: "user", id: r.id, text: r.text, selection: r.selection ?? null };
      case "assistant":
        return { kind: "assistant", id: r.id, text: r.text };
      case "tool":
        return { kind: "tool", id: r.id, name: r.name, summary: r.summary };
      case "draft":
        return { kind: "draft", id: `draft-${r.draft.id}`, draft: r.draft };
      default:
        return { kind: "error", id: `h-error-${i}`, text: r.text };
    }
  });
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "reset":
      return initialChat;
    case "status":
      return { ...state, status: action.status };
    case "sent": {
      const seq = state.seq + 1;
      const item: ChatItem = { kind: "user", id: `local-${seq}`, text: action.text, selection: action.selection };
      return { ...state, seq, busy: true, items: [...state.items, item] };
    }
    case "server":
      return onServer(state, action.event);
  }
}

function onServer(state: ChatState, e: ServerEvent): ChatState {
  const seq = state.seq + 1;
  switch (e.type) {
    case "history":
      return { ...state, seq, items: fromHistory(e.messages), busy: e.busy };
    case "assistant_text": {
      const last = state.items[state.items.length - 1];
      if (last && last.kind === "assistant" && last.id === e.message_id) {
        const items = state.items.slice(0, -1);
        items.push({ ...last, text: last.text + e.text });
        return { ...state, seq, busy: true, items };
      }
      return { ...state, seq, busy: true, items: [...state.items, { kind: "assistant", id: e.message_id, text: e.text }] };
    }
    case "tool_use":
      return {
        ...state,
        seq,
        busy: true,
        items: [...state.items, { kind: "tool", id: e.message_id, name: e.name, summary: e.summary }],
      };
    case "draft":
      return { ...state, seq, items: [...state.items, { kind: "draft", id: `draft-${e.draft.id}`, draft: e.draft }] };
    case "turn_done":
      return {
        ...state,
        seq,
        busy: false,
        lastTurn: {
          cost_usd: e.cost_usd ?? null,
          session_cost_usd: e.session_cost_usd ?? null,
          duration_s: e.duration_s ?? null,
        },
      };
    case "error": {
      const item: ChatItem = { kind: "error", id: `error-${seq}`, text: e.message };
      return { ...state, seq, busy: e.message.startsWith(STILL_BUSY) ? state.busy : false, items: [...state.items, item] };
    }
    default:
      return state;
  }
}

/** Friendlier names for the pair agent's tools; the server's summary carries the detail. */
const TOOL_VERBS: Record<string, string> = {
  search_builder_transcript: "Searching builder transcript",
  open_excerpt: "Reading transcript excerpt",
  get_file_diff: "Reading the file's diff",
  read_base_file: "Reading the file before this PR",
  get_pr_comments: "Reading PR comments",
  draft_review_comment: "Drafting a comment",
  Read: "Reading",
  Grep: "Searching code",
  Glob: "Listing files",
};

export function toolLabel(name: string, summary: string): { verb: string; detail: string } {
  const short = name.replace(/^mcp__pair__/, "");
  const verb = TOOL_VERBS[short] ?? short;
  const detail = summary.replace(/^(search transcripts:|open|Read|Grep|Glob|get_file_diff|read_base_file|draft comment on)\s*/, "");
  return { verb, detail: detail === short ? "" : detail };
}
