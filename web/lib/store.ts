import { create } from "zustand";

import { type ChatAction, type ChatState, chatReducer, initialChat } from "./chat";
import type { Draft, Selection, Side } from "./types";

export interface ScrollRequest {
  path: string;
  line?: number;
  side?: Side;
  nonce: number;
}

interface ReviewStore {
  selection: Selection | null;
  setSelection(s: Selection | null): void;

  chat: ChatState;
  dispatchChat(action: ChatAction): void;

  drafts: Draft[];
  setDrafts(drafts: Draft[]): void;
  upsertDraft(draft: Draft): void;

  /** The lines a new comment is being written on. The text is kept apart so typing doesn't touch the diff. */
  composing: Selection | null;
  setComposing(target: Selection | null): void;
  composeBody: string;
  setComposeBody(body: string): void;

  scrollRequest: ScrollRequest | null;
  scrollTo(path: string, line?: number, side?: Side): void;

  excerptRef: string | null;
  openExcerpt(ref: string | null): void;

  draftsOpen: boolean;
  setDraftsOpen(open: boolean): void;

  reset(): void;
}

let nonce = 0;

export const useReview = create<ReviewStore>((set) => ({
  selection: null,
  setSelection: (selection) => set({ selection }),

  chat: initialChat,
  dispatchChat: (action) =>
    set((s) => {
      const chat = chatReducer(s.chat, action);
      if (action.type === "server" && action.event.type === "draft") {
        return { chat, drafts: upsert(s.drafts, action.event.draft) };
      }
      return { chat };
    }),

  drafts: [],
  setDrafts: (drafts) => set({ drafts }),
  upsertDraft: (draft) => set((s) => ({ drafts: upsert(s.drafts, draft) })),

  composing: null,
  setComposing: (composing) => set({ composing, composeBody: "" }),
  composeBody: "",
  setComposeBody: (composeBody) => set({ composeBody }),

  scrollRequest: null,
  scrollTo: (path, line, side) => set({ scrollRequest: { path, line, side, nonce: ++nonce } }),

  excerptRef: null,
  openExcerpt: (excerptRef) => set({ excerptRef }),

  draftsOpen: false,
  setDraftsOpen: (draftsOpen) => set({ draftsOpen }),

  reset: () =>
    set({
      selection: null,
      chat: initialChat,
      drafts: [],
      composing: null,
      composeBody: "",
      scrollRequest: null,
      excerptRef: null,
      draftsOpen: false,
    }),
}));

function upsert(drafts: Draft[], d: Draft): Draft[] {
  const i = drafts.findIndex((x) => x.id === d.id);
  if (i < 0) return [...drafts, d];
  const next = drafts.slice();
  next[i] = d;
  return next;
}

/** Drafts still waiting on the reviewer: neither discarded nor posted. */
export function openDrafts(drafts: Draft[]): Draft[] {
  return drafts.filter((d) => d.status === "draft" || d.status === "approved");
}
