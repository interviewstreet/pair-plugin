"use client";

import { useEffect, useRef } from "react";

import { useReview } from "@/lib/store";
import type { Selection } from "@/lib/types";

import { ChatMessage } from "./ChatMessage";
import { Composer } from "./Composer";
import { ActivityDots } from "./ui";

const STATUS_TEXT = {
  connecting: "Connecting",
  open: "Pair agent",
  reconnecting: "Reconnecting",
  closed: "Disconnected",
  refused: "Chat unavailable",
} as const;

export function ChatPane({
  onSend,
  onStop,
}: {
  onSend: (text: string, selection: Selection | null) => boolean;
  onStop: () => void;
}) {
  const chat = useReview((s) => s.chat);
  const logRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    const el = logRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [chat.items, chat.busy]);

  const last = chat.lastTurn;

  return (
    <div className="chat-pane">
      <div className="pane-head">
        <span className={`conn-dot ${chat.status}`} />
        <span className="pane-title">{STATUS_TEXT[chat.status]}</span>
        {chat.busy && (
          <span className="busy">
            <ActivityDots />
            Thinking
          </span>
        )}
        <span className="spacer" />
        {last && !chat.busy && (
          <span className="turn-stats">
            {last.duration_s != null && `${last.duration_s.toFixed(1)}s`}
            {last.session_cost_usd != null && ` · $${last.session_cost_usd.toFixed(2)}`}
          </span>
        )}
      </div>
      <div
        className="chat-log"
        ref={logRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
      >
        {chat.items.length === 0 ? (
          <div className="chat-empty">
            <p className="chat-empty-title">Ask about this PR</p>
            <p className="chat-empty-hint">Select line numbers in the diff to attach them to your question.</p>
          </div>
        ) : (
          chat.items.map((item, i) => <ChatMessage key={`${i}:${item.id}`} item={item} />)
        )}
      </div>
      <Composer onSend={onSend} onStop={onStop} />
    </div>
  );
}
