import { describe, expect, it } from "vitest";

import { type ChatState, chatReducer, initialChat, toolLabel } from "@/lib/chat";
import { useReview } from "@/lib/store";
import type { Draft, ServerEvent } from "@/lib/types";

const run = (events: ServerEvent[], from: ChatState = initialChat) =>
  events.reduce((s, event) => chatReducer(s, { type: "server", event }), from);

const draft: Draft = {
  id: "d1",
  path: "src/greet.py",
  side: "additions",
  line: 4,
  body: "Consider a default.",
  status: "draft",
  created_at: "2026-01-02T11:00:00Z",
};

describe("chatReducer", () => {
  it("replaces items with history and takes busy from it", () => {
    const s = run([
      {
        type: "history",
        busy: true,
        messages: [
          { role: "user", id: "u1", text: "why?", selection: { path: "a.py", side: "additions", start: 1, end: 2 } },
          { role: "assistant", id: "m1", text: "because" },
          { role: "tool", id: "t1", name: "mcp__pair__search_builder_transcript", summary: "search transcripts: loud" },
          { role: "draft", draft },
          { role: "error", text: "boom" },
        ],
      },
    ]);
    expect(s.busy).toBe(true);
    expect(s.items.map((i) => i.kind)).toEqual(["user", "assistant", "tool", "draft", "error"]);
    expect(run([{ type: "history", busy: false, messages: [] }], s).items).toEqual([]);
  });

  it("appends assistant deltas to the same message and starts a new one for a new id", () => {
    const s = run([
      { type: "assistant_text", message_id: "m1", text: "Hel" },
      { type: "assistant_text", message_id: "m1", text: "lo" },
      { type: "tool_use", message_id: "t1", name: "Read", summary: "Read src/greet.py" },
      { type: "assistant_text", message_id: "m2", text: "Done" },
    ]);
    expect(s.items).toEqual([
      { kind: "assistant", id: "m1", text: "Hello" },
      { kind: "tool", id: "t1", name: "Read", summary: "Read src/greet.py" },
      { kind: "assistant", id: "m2", text: "Done" },
    ]);
    expect(s.busy).toBe(true);
  });

  it("tracks busy across a turn", () => {
    let s = chatReducer(initialChat, { type: "sent", text: "hi", selection: null });
    expect(s.busy).toBe(true);
    expect(s.items[0]).toMatchObject({ kind: "user", text: "hi", selection: null });
    s = run([{ type: "turn_done", cost_usd: 0.012, session_cost_usd: 0.05, duration_s: 7.5 }], s);
    expect(s.busy).toBe(false);
    expect(s.lastTurn).toEqual({ cost_usd: 0.012, session_cost_usd: 0.05, duration_s: 7.5 });
  });

  it("stays busy when a message was refused mid-turn, and stops on other errors", () => {
    const busy = chatReducer(initialChat, { type: "sent", text: "hi", selection: null });
    const refused = run([{ type: "error", message: "the pair agent is still answering; interrupt it or wait" }], busy);
    expect(refused.busy).toBe(true);
    const failed = run([{ type: "error", message: "pair agent failed: boom" }], busy);
    expect(failed.busy).toBe(false);
    expect(failed.items.at(-1)).toMatchObject({ kind: "error", text: "pair agent failed: boom" });
  });

  it("records connection status", () => {
    expect(chatReducer(initialChat, { type: "status", status: "reconnecting" }).status).toBe("reconnecting");
  });
});

describe("store chat dispatch", () => {
  it("adds live drafts to the drafts list", () => {
    useReview.getState().dispatchChat({ type: "server", event: { type: "draft", draft } });
    useReview.getState().dispatchChat({ type: "server", event: { type: "draft", draft: { ...draft, body: "edited" } } });
    expect(useReview.getState().drafts).toEqual([{ ...draft, body: "edited" }]);
    expect(useReview.getState().chat.items.filter((i) => i.kind === "draft")).toHaveLength(2);
  });
});

describe("toolLabel", () => {
  it("names the pair agent's tools", () => {
    expect(toolLabel("mcp__pair__search_builder_transcript", "search transcripts: layout")).toEqual({
      verb: "Searching builder transcript",
      detail: "layout",
    });
    expect(toolLabel("Grep", "Grep KillSession in internal")).toEqual({ verb: "Searching code", detail: "KillSession in internal" });
    expect(toolLabel("mcp__pair__open_excerpt", "open claude:s:u")).toEqual({ verb: "Reading transcript excerpt", detail: "claude:s:u" });
    expect(toolLabel("SomethingNew", "SomethingNew")).toEqual({ verb: "SomethingNew", detail: "" });
  });
});
