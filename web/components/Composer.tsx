"use client";

import { type KeyboardEvent, useState } from "react";

import { selectionLabel } from "@/lib/selection";
import { useReview } from "@/lib/store";
import type { Selection } from "@/lib/types";

import { ArrowUp, Square, X } from "./icons";
import { Button } from "./ui";

export function Composer({
  onSend,
  onStop,
}: {
  onSend: (text: string, selection: Selection | null) => boolean;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const selection = useReview((s) => s.selection);
  const setSelection = useReview((s) => s.setSelection);
  const setComposing = useReview((s) => s.setComposing);
  const scrollTo = useReview((s) => s.scrollTo);
  const busy = useReview((s) => s.chat.busy);
  const status = useReview((s) => s.chat.status);
  const canSend = !!text.trim() && !busy && status === "open";

  const send = () => {
    if (!canSend) return;
    if (onSend(text.trim(), selection)) {
      setText("");
      setSelection(null);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="composer">
      <div className="composer-card">
        {selection && (
          <span className="sel-chip" data-testid="selection-chip">
            <span className="sel-label">{selectionLabel(selection)}</span>
            <button
              type="button"
              className="chip-action"
              onClick={() => {
                setComposing(selection);
                scrollTo(selection.path, selection.end, selection.side);
              }}
            >
              Comment
            </button>
            <button type="button" className="chip-x" aria-label="Remove selection" onClick={() => setSelection(null)}>
              <X size={11} />
            </button>
          </span>
        )}
        <textarea
          aria-label="Message the pair agent"
          rows={2}
          placeholder={selection ? "Ask about the selected lines…" : "Ask the pair agent about this PR…"}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="composer-foot">
          <span className="composer-hint">⏎ to send · ⇧⏎ for a new line</span>
          {busy ? (
            <Button variant="secondary" size="icon" aria-label="Stop" title="Stop" onClick={onStop}>
              <Square size={12} fill="currentColor" />
            </Button>
          ) : (
            <Button variant="primary" size="icon" aria-label="Send" title="Send" disabled={!canSend} onClick={send}>
              <ArrowUp size={14} />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
