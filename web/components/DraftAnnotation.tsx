"use client";

import { useState } from "react";

import { draftLocation } from "@/lib/drafts";
import type { Draft } from "@/lib/types";
import { useDraftActions } from "@/lib/useDraftActions";

import { Markdown } from "./Markdown";
import { Button, Tag, type TagTone } from "./ui";

const STATUS: Record<Draft["status"], [string, TagTone]> = {
  draft: ["draft", "neutral"],
  approved: ["approved", "success"],
  discarded: ["discarded", "neutral"],
  posted: ["posted", "info"],
};

export function DraftAnnotation({
  draft,
  reviewKey,
  showLocation = false,
  onLocate,
}: {
  draft: Draft;
  reviewKey: string;
  showLocation?: boolean;
  onLocate?: () => void;
}) {
  const { patch } = useDraftActions(reviewKey);
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(draft.body);
  const [busy, setBusy] = useState(false);
  const [label, tone] = STATUS[draft.status] ?? [draft.status, "neutral"];

  const run = async (change: Parameters<typeof patch>[1]) => {
    setBusy(true);
    const ok = await patch(draft.id, change);
    setBusy(false);
    return ok;
  };

  return (
    <div className={`draft-card ${draft.status}`} data-draft-id={draft.id}>
      <div className="head">
        {showLocation ? (
          <button type="button" className="loc" onClick={onLocate}>
            {draftLocation(draft)}
          </button>
        ) : (
          <span>{draft.author === "user" ? "Your comment" : "Pair agent's draft"}</span>
        )}
        <Tag tone={tone}>{label}</Tag>
      </div>
      {editing ? (
        <textarea
          aria-label="Comment body"
          rows={Math.min(12, Math.max(3, body.split("\n").length + 1))}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          autoFocus
        />
      ) : (
        <div className="draft-body">
          <Markdown text={draft.body} />
        </div>
      )}
      {draft.status !== "posted" && (
        <div className="actions">
          {editing ? (
            <>
              <Button
                variant="primary"
                loading={busy}
                disabled={!body.trim() || body === draft.body}
                onClick={async () => {
                  if (await run({ body })) setEditing(false);
                }}
              >
                Save
              </Button>
              <Button
                variant="ghost"
                onClick={() => {
                  setBody(draft.body);
                  setEditing(false);
                }}
              >
                Cancel
              </Button>
            </>
          ) : draft.status === "discarded" ? (
            <Button variant="ghost" disabled={busy} onClick={() => run({ status: "draft" })}>
              Restore
            </Button>
          ) : (
            <>
              {draft.status === "approved" ? (
                <Button variant="secondary" disabled={busy} onClick={() => run({ status: "draft" })}>
                  Unapprove
                </Button>
              ) : (
                <Button variant="primary" disabled={busy} onClick={() => run({ status: "approved" })}>
                  Approve
                </Button>
              )}
              <Button
                variant="ghost"
                onClick={() => {
                  setBody(draft.body);
                  setEditing(true);
                }}
              >
                Edit
              </Button>
              <Button variant="ghost" disabled={busy} onClick={() => run({ status: "discarded" })}>
                Discard
              </Button>
            </>
          )}
        </div>
      )}
      {draft.status === "posted" && draft.review_url && (
        <div className="actions">
          <a className="text-link" href={draft.review_url} target="_blank" rel="noreferrer">
            View on GitHub
          </a>
        </div>
      )}
    </div>
  );
}
