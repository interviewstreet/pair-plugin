"use client";

import { type KeyboardEvent, useState } from "react";

import { api } from "@/lib/api";
import { draftLocation, newDraftFor } from "@/lib/drafts";
import { useReview } from "@/lib/store";
import type { Selection } from "@/lib/types";
import { useNotify } from "@/lib/useNotify";

import { Button } from "./ui";

export function NewComment({ reviewKey, target }: { reviewKey: string; target: Selection }) {
  const body = useReview((s) => s.composeBody);
  const setBody = useReview((s) => s.setComposeBody);
  const setComposing = useReview((s) => s.setComposing);
  const setSelection = useReview((s) => s.setSelection);
  const upsertDraft = useReview((s) => s.upsertDraft);
  const notify = useNotify();
  const [busy, setBusy] = useState(false);
  const req = newDraftFor(target, body);

  const close = () => {
    setComposing(null);
    setSelection(null);
  };

  const save = async () => {
    if (!req.body || busy) return;
    setBusy(true);
    try {
      upsertDraft(await api.createDraft(reviewKey, req));
      close();
    } catch (e) {
      notify("error", `Couldn't save the comment: ${(e as Error).message}`);
      setBusy(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void save();
    } else if (e.key === "Escape") {
      close();
    }
  };

  return (
    <div className="draft-card new-comment" data-testid="new-comment">
      <div className="head">
        <span>Comment on {draftLocation(req)}</span>
      </div>
      <textarea
        aria-label="New comment"
        rows={3}
        placeholder="Leave a comment…"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        onKeyDown={onKeyDown}
        autoFocus
      />
      <div className="actions">
        <Button variant="primary" loading={busy} disabled={!req.body} onClick={save}>
          Add comment
        </Button>
        <Button variant="ghost" onClick={close}>
          Cancel
        </Button>
        <span className="kbd-hint">⌘⏎</span>
      </div>
    </div>
  );
}
