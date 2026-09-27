"use client";

import type { SessionInfo } from "@/lib/types";

import { formatWhen } from "./BriefSection";
import { Tag } from "./ui";

const ROLE_LABELS: Record<string, string> = { build: "built the code", discussion: "discussion only" };

export function SessionsList({ sessions }: { sessions: SessionInfo[] }) {
  if (!sessions.length) return <p className="muted">No matching sessions.</p>;
  return (
    <ul className="sessions">
      {sessions.map((s) => (
        <li key={`${s.agent}:${s.id}`}>
          <div className="session-line">
            <span className="session-agent">{s.agent}</span>
            <span className="session-title" title={s.id}>
              {s.title ? s.title : <span className="mono">{s.id.slice(0, 8)}</span>}
            </span>
            {s.started_at && <span className="session-when">{formatWhen(s.started_at)}</span>}
          </div>
          <div className="session-meta">
            <span>{ROLE_LABELS[s.role] ?? s.role}</span>
            {s.opened_pr && <Tag tone="info">opened the PR</Tag>}
          </div>
        </li>
      ))}
    </ul>
  );
}
