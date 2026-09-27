"use client";

import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { type ImperativePanelHandle, Panel, PanelGroup } from "react-resizable-panels";

import { ApiError, api } from "@/lib/api";
import { useReview } from "@/lib/store";
import type { CloseResult } from "@/lib/types";
import { useChatSocket } from "@/lib/useChatSocket";
import { useIsNarrow } from "@/lib/useMediaQuery";

import { BriefDrawer, BriefPane, BriefRail } from "./BriefPane";
import { ChatPane } from "./ChatPane";
import { DiffPane } from "./DiffPane";
import { DraftsDrawer } from "./DraftsDrawer";
import { ExcerptDrawer } from "./ExcerptDrawer";
import { PathsContext } from "./Markdown";
import { ResizeHandle } from "./ResizeHandle";
import { TopBar } from "./TopBar";
import { Spinner } from "./ui";

function StatusPage({ children }: { children: React.ReactNode }) {
  return (
    <div className="status-page">
      <div>{children}</div>
    </div>
  );
}

export function ReviewPage({ reviewKey }: { reviewKey: string }) {
  const [closed, setClosed] = useState<CloseResult | null>(null);
  const reset = useReview((s) => s.reset);
  const setDrafts = useReview((s) => s.setDrafts);
  const isNarrow = useIsNarrow();
  const scrollRequest = useReview((s) => s.scrollRequest);
  const [briefOpen, setBriefOpen] = useState(false);
  const briefPanel = useRef<ImperativePanelHandle>(null);
  const briefWidth = useRef(30);

  useEffect(() => reset(), [reviewKey, reset]);

  // The drawer covers the diff on narrow screens, so get it out of the way when something scrolls the diff.
  useEffect(() => {
    if (scrollRequest && isNarrow) setBriefOpen(false);
  }, [scrollRequest, isNarrow]);

  const toggleBrief = useCallback(() => {
    const panel = briefPanel.current;
    if (isNarrow || !panel) setBriefOpen((open) => !open);
    else if (panel.isCollapsed()) panel.resize(briefWidth.current);
    else panel.collapse();
  }, [isNarrow]);

  const meta = useQuery({ queryKey: ["meta", reviewKey], queryFn: () => api.meta(reviewKey), enabled: !closed });
  const brief = useQuery({
    queryKey: ["brief", reviewKey],
    queryFn: () => api.brief(reviewKey),
    enabled: !closed,
    refetchInterval: (q) => ((q.state.data?.pending?.length ?? 0) > 0 ? 2000 : false),
  });
  const diff = useQuery({ queryKey: ["diff", reviewKey], queryFn: () => api.diff(reviewKey), enabled: !closed, staleTime: Infinity });
  const risky = useQuery({ queryKey: ["risky", reviewKey], queryFn: () => api.risky(reviewKey), enabled: !closed, staleTime: Infinity });
  const drafts = useQuery({ queryKey: ["drafts", reviewKey], queryFn: () => api.drafts(reviewKey), enabled: !closed });

  useEffect(() => {
    if (drafts.data) setDrafts(drafts.data);
  }, [drafts.data, setDrafts]);

  const refetchDrafts = drafts.refetch;
  const chat = useChatSocket(closed ? null : reviewKey, () => void refetchDrafts());

  const paths = useMemo(() => meta.data?.files.map((f) => f.path) ?? [], [meta.data]);
  useEffect(() => {
    if (meta.data) document.title = `${meta.data.pr.repo}#${meta.data.pr.number} · pair review`;
  }, [meta.data]);

  const closeReview = useCallback(async () => {
    chat.close();
    setClosed(await api.close(reviewKey));
  }, [chat, reviewKey]);

  if (closed) {
    return (
      <StatusPage>
        <h2>Review closed</h2>
        <p className="muted">
          The pair agent session ended{closed.worktree_removed ? " and the worktree was removed" : ""}. Reload to start a new
          session.
        </p>
        <p>
          <a href="/">All reviews</a>
        </p>
      </StatusPage>
    );
  }

  const notFound = [meta.error, brief.error].find((e) => e instanceof ApiError && e.status === 404);
  if (notFound) {
    return (
      <StatusPage>
        <h2>No brief for {reviewKey}</h2>
        <p className="muted">
          Run <code>/pair {reviewKey.replace(/__/, "/").replace(/__/, "#")}</code> in Claude Code to build one.
        </p>
        <p>
          <a href="/">All reviews</a>
        </p>
      </StatusPage>
    );
  }

  const failed = meta.error ?? brief.error;
  if (failed) {
    return (
      <StatusPage>
        <h2>Couldn&apos;t load this review</h2>
        <p className="muted">{failed.message}</p>
      </StatusPage>
    );
  }

  if (!meta.data || !brief.data) {
    return (
      <StatusPage>
        <Spinner label="Loading review" />
      </StatusPage>
    );
  }

  const sessions = meta.data.sessions;
  const chatAndDiff = (
    <PanelGroup
      className={isNarrow ? "panes" : undefined}
      direction={isNarrow ? "vertical" : "horizontal"}
      autoSaveId={isNarrow ? "pair:workspace:narrow" : "pair:workspace"}
    >
      <Panel className="chat-column" defaultSize={isNarrow ? 45 : 32} minSize={isNarrow ? 25 : 20} maxSize={isNarrow ? 75 : 60}>
        <ChatPane onSend={chat.send} onStop={chat.interrupt} />
      </Panel>
      <ResizeHandle direction={isNarrow ? "vertical" : "horizontal"} label="Resize chat and diff" />
      <Panel minSize={25}>
        {diff.data !== undefined ? (
          <DiffPane
            reviewKey={reviewKey}
            headSha={meta.data.head_sha}
            patch={diff.data}
            risky={risky.data ?? []}
            files={meta.data.files}
          />
        ) : (
          <div className="pane-status">
            {diff.isError ? `Couldn't load the diff: ${diff.error.message}` : <Spinner label="Loading diff" />}
          </div>
        )}
      </Panel>
    </PanelGroup>
  );

  return (
    <PathsContext.Provider value={paths}>
      <div className="app">
        <TopBar meta={meta.data} onCloseReview={closeReview} />
        <div className="workspace">
          {isNarrow ? (
            chatAndDiff
          ) : (
            // Only the chat/diff split is saved, so the brief starts collapsed on every load.
            <PanelGroup className="panes" direction="horizontal">
              <Panel defaultSize={100} minSize={40}>
                {chatAndDiff}
              </Panel>
              <ResizeHandle label="Resize diff and brief" />
              <Panel
                ref={briefPanel}
                className="brief-pane"
                collapsible
                collapsedSize={0}
                defaultSize={0}
                minSize={20}
                maxSize={45}
                onCollapse={() => setBriefOpen(false)}
                onExpand={() => setBriefOpen(true)}
                onResize={(size) => {
                  if (size > 0) briefWidth.current = size;
                }}
              >
                <BriefPane brief={brief.data} sessions={sessions} open={briefOpen} />
              </Panel>
            </PanelGroup>
          )}
          <BriefRail
            open={briefOpen}
            pending={(brief.data.pending?.length ?? 0) > 0}
            divider={isNarrow || briefOpen}
            onToggle={toggleBrief}
          />
        </div>
        {isNarrow && (
          <BriefDrawer brief={brief.data} sessions={sessions} open={briefOpen} onClose={() => setBriefOpen(false)} />
        )}
        <ExcerptDrawer reviewKey={reviewKey} />
        <DraftsDrawer reviewKey={reviewKey} meta={meta.data} />
      </div>
    </PathsContext.Provider>
  );
}
