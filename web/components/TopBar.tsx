"use client";

import { HRConfirm } from "@hackerrank/hrds-components";
import { useState } from "react";

import { openDrafts, useReview } from "@/lib/store";
import type { ReviewMeta } from "@/lib/types";

import { ExternalLink, MessageSquare } from "./icons";
import { Button } from "./ui";

export function TopBar({ meta, onCloseReview }: { meta: ReviewMeta; onCloseReview: () => Promise<void> }) {
  const drafts = useReview((s) => s.drafts);
  const setDraftsOpen = useReview((s) => s.setDraftsOpen);
  const [confirming, setConfirming] = useState(false);
  const [closing, setClosing] = useState(false);
  const pr = meta.pr;
  const open = openDrafts(drafts).length;
  const repoName = pr.repo.split("/").pop() ?? pr.repo;

  return (
    <header className="topbar">
      <a
        className="pr-link"
        href={pr.url}
        target="_blank"
        rel="noreferrer"
        title={`${pr.repo}#${pr.number} · ${pr.head_branch} → ${pr.base_branch} · ${meta.head_sha.slice(0, 7)}`}
      >
        <span className="pr-ref">
          {repoName}
          <span className="pr-num">#{pr.number}</span>
        </span>
        <span className="pr-title">{pr.title}</span>
        <ExternalLink size={12} className="pr-ext" />
      </a>
      <span className="spacer" />
      <div className="actions">
        <Button variant="ghost" onClick={() => setDraftsOpen(true)}>
          <MessageSquare size={13} />
          Drafts
          {open > 0 && <span className="count-badge">{open}</span>}
        </Button>
        <Button variant="ghost" loading={closing} onClick={() => setConfirming(true)}>
          Close review
        </Button>
      </div>
      <HRConfirm.Root open={confirming} onOpenChange={setConfirming}>
        <HRConfirm.Content>
          <HRConfirm.Header title="Close this review?" />
          <HRConfirm.Body>
            This ends the pair agent session and removes the worktree. The brief and drafts stay on disk.
          </HRConfirm.Body>
          <div className="confirm-footer">
            <HRConfirm.ConfirmCta type="cancel">
              <Button variant="ghost" size="md">
                Cancel
              </Button>
            </HRConfirm.ConfirmCta>
            <HRConfirm.ConfirmCta type="action">
              <Button
                variant="danger"
                size="md"
                onClick={async () => {
                  setClosing(true);
                  try {
                    await onCloseReview();
                  } finally {
                    setClosing(false);
                  }
                }}
              >
                Close review
              </Button>
            </HRConfirm.ConfirmCta>
          </div>
        </HRConfirm.Content>
      </HRConfirm.Root>
    </header>
  );
}
