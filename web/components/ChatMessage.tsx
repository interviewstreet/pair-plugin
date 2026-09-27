"use client";

import { memo } from "react";

import type { ChatItem } from "@/lib/chat";
import { toolLabel } from "@/lib/chat";
import { draftLocation } from "@/lib/drafts";
import { selectionLabel } from "@/lib/selection";
import { useReview } from "@/lib/store";

import { ChevronRight } from "./icons";
import { Markdown } from "./Markdown";

function ChatMessageImpl({ item }: { item: ChatItem }) {
  const scrollTo = useReview((s) => s.scrollTo);
  const setDraftsOpen = useReview((s) => s.setDraftsOpen);

  switch (item.kind) {
    case "user":
      return (
        <div className="msg user">
          {item.selection && (
            <button
              type="button"
              className="ref-chip"
              onClick={() => scrollTo(item.selection!.path, item.selection!.start, item.selection!.side)}
            >
              {selectionLabel(item.selection)}
            </button>
          )}
          <div className="text">{item.text}</div>
        </div>
      );
    case "assistant":
      return (
        <div className="msg assistant">
          <Markdown text={item.text} />
        </div>
      );
    case "tool": {
      const { verb, detail } = toolLabel(item.name, item.summary);
      return (
        <div className="msg tool" data-tool={item.name}>
          <ChevronRight size={11} />
          <span className="verb">{verb}</span>
          {detail && <span className="detail">{detail}</span>}
        </div>
      );
    }
    case "draft":
      return (
        <div className="msg draft-note">
          <span>Drafted a comment on</span>
          <button
            type="button"
            className="ref-chip"
            onClick={() => scrollTo(item.draft.path, item.draft.line, item.draft.side)}
          >
            {draftLocation(item.draft)}
          </button>
          <button type="button" className="text-link" onClick={() => setDraftsOpen(true)}>
            Open drafts
          </button>
        </div>
      );
    case "error":
      return <div className="msg error">{item.text}</div>;
  }
}

export const ChatMessage = memo(ChatMessageImpl);
