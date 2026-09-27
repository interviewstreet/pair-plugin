"use client";

import { HRDrawer } from "@hackerrank/hrds-components";
import { useState } from "react";

import { ApiError, api } from "@/lib/api";
import { draftsMarkdown, footerAction } from "@/lib/drafts";
import { useReview } from "@/lib/store";
import type { Draft, ReviewMeta } from "@/lib/types";
import { useNotify } from "@/lib/useNotify";

import { DraftAnnotation } from "./DraftAnnotation";
import { Button } from "./ui";

const ORDER: Record<Draft["status"], number> = { approved: 0, draft: 1, posted: 2, discarded: 3 };

export function DraftsFooter({
  meta,
  drafts,
  onPost,
  onCopy,
  posting,
}: {
  meta: Pick<ReviewMeta, "can_post" | "post_account">;
  drafts: Draft[];
  onPost: () => void;
  onCopy: () => void;
  posting?: boolean;
}) {
  const action = footerAction(meta.can_post, drafts, meta.post_account);
  if (action.kind === "post") {
    return (
      <Button variant="primary" size="md" disabled={action.approved === 0} loading={posting} onClick={onPost}>
        {`Post approved (${action.approved})${action.account ? ` as ${action.account}` : ""}`}
      </Button>
    );
  }
  return (
    <Button variant="primary" size="md" disabled={action.count === 0} onClick={onCopy}>
      {`Copy all (${action.count})`}
    </Button>
  );
}

export function DraftsDrawer({ reviewKey, meta }: { reviewKey: string; meta: ReviewMeta }) {
  const open = useReview((s) => s.draftsOpen);
  const setOpen = useReview((s) => s.setDraftsOpen);
  const drafts = useReview((s) => s.drafts);
  const setDrafts = useReview((s) => s.setDrafts);
  const scrollTo = useReview((s) => s.scrollTo);
  const notify = useNotify();
  const [posting, setPosting] = useState(false);

  const sorted = [...drafts].sort((a, b) => ORDER[a.status] - ORDER[b.status] || a.created_at.localeCompare(b.created_at));

  const post = async () => {
    const approved = drafts.filter((d) => d.status === "approved").length;
    if (!window.confirm(`Post ${approved} approved comment(s) to ${meta.pr.repo}#${meta.pr.number} on GitHub?`)) return;
    setPosting(true);
    try {
      const res = await api.post(reviewKey);
      setDrafts(res.drafts);
      notify(
        "success",
        <span>
          Posted.{" "}
          <a href={res.review_url} target="_blank" rel="noreferrer">
            Open the review
          </a>
        </span>,
        10_000,
      );
    } catch (e) {
      notify("error", `Couldn't post: ${e instanceof ApiError ? e.message : String(e)}`);
    } finally {
      setPosting(false);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(draftsMarkdown(drafts, meta.pr, meta.head_sha));
      notify("success", "Copied the drafts as markdown.");
    } catch (e) {
      notify("error", `Couldn't copy: ${(e as Error).message}`);
    }
  };

  return (
    <HRDrawer.Root isOpen={open} onClose={() => setOpen(false)} title={`Drafts (${drafts.length})`} position="right" size="lg">
      <HRDrawer.Body>
        {drafts.length === 0 ? (
          <div className="drawer-empty">
            <p className="drawer-empty-title">No drafts yet</p>
            <p className="muted">
              Ask the pair agent to draft one, like &ldquo;draft a comment about this&rdquo;, or select lines and click
              Comment.
            </p>
          </div>
        ) : (
          <div className="drafts-list">
            {sorted.map((d) => (
              <DraftAnnotation
                key={d.id}
                draft={d}
                reviewKey={reviewKey}
                showLocation
                onLocate={() => {
                  setOpen(false);
                  scrollTo(d.path, d.line, d.side);
                }}
              />
            ))}
          </div>
        )}
      </HRDrawer.Body>
      <HRDrawer.Footer align="right" hasDivider>
        <span className="drawer-footer-note">
          {meta.can_post
            ? "Approved drafts post as one review."
            : `Posting is off for ${meta.pr.repo}; copy the drafts into GitHub yourself.`}
        </span>
        <DraftsFooter meta={meta} drafts={drafts} onPost={post} onCopy={copy} posting={posting} />
      </HRDrawer.Footer>
    </HRDrawer.Root>
  );
}
