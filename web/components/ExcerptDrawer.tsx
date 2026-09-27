"use client";

import { HRDrawer, HRHint, HRHintVariant } from "@hackerrank/hrds-components";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api } from "@/lib/api";
import { parseRef } from "@/lib/refs";
import { useReview } from "@/lib/store";

import { formatWhen } from "./BriefSection";
import { Markdown } from "./Markdown";
import { AGENT_NAMES, AgentMark, Transcript } from "./Transcript";
import { Spinner } from "./ui";

const ROLE_LABELS: Record<string, string> = { build: "Built the code", discussion: "Discussion only" };

export function ExcerptDrawer({ reviewKey }: { reviewKey: string }) {
  const ref = useReview((s) => s.excerptRef);
  const openExcerpt = useReview((s) => s.openExcerpt);
  const focusRef = useRef<HTMLDivElement>(null);

  const excerpt = useQuery({
    queryKey: ["excerpt", reviewKey, ref],
    queryFn: () => api.excerpt(reviewKey, ref!),
    enabled: !!ref,
    staleTime: Infinity,
  });

  useEffect(() => {
    if (excerpt.data) requestAnimationFrame(() => focusRef.current?.scrollIntoView({ block: "center" }));
  }, [excerpt.data]);

  const parsed = ref ? parseRef(ref) : null;
  const agent = excerpt.data?.agent ?? (parsed?.kind === "claude" || parsed?.kind === "codex" ? parsed.kind : "");
  const title = parsed?.kind === "pr" ? "PR description" : `${AGENT_NAMES[agent] ?? "Agent"} transcript`;
  const d = excerpt.data;
  const role = d?.session_role ? (ROLE_LABELS[d.session_role] ?? d.session_role) : "";

  return (
    <HRDrawer.Root isOpen={!!ref} onClose={() => openExcerpt(null)} title={title} position="right" size="lg">
      <HRDrawer.Body>
        {excerpt.isPending && ref && (
          <div className="tx-loading">
            <Spinner label="Loading the excerpt" />
          </div>
        )}
        {excerpt.isError && <HRHint variant={HRHintVariant.ERROR}>Couldn&apos;t open {ref}: {excerpt.error.message}</HRHint>}
        {d && parsed?.kind === "pr" && d.messages.map((m, i) => <Markdown key={i} text={m.text} />)}
        {d && parsed?.kind !== "pr" && (
          <>
            {d.session_id && (
              <div className="tx-meta">
                <div className="tx-meta-row">
                  <AgentMark agent={agent} />
                  <span className="tx-meta-agent">{AGENT_NAMES[agent] ?? agent} session</span>
                  {role && <span className="tx-meta-tag">{role}</span>}
                  {d.timestamp && <span className="tx-subtle">{formatWhen(d.timestamp)}</span>}
                </div>
                {d.session_title && <div className="tx-meta-title">{d.session_title.replace(/^#+\s*/, "")}</div>}
                <div className="tx-meta-ref mono">{d.ref}</div>
              </div>
            )}
            <Transcript
              agent={agent}
              messages={d.messages}
              skipped={d.skipped}
              moreAfter={d.more_after}
              focusRef={focusRef}
            />
          </>
        )}
      </HRDrawer.Body>
    </HRDrawer.Root>
  );
}
