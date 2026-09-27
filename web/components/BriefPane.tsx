"use client";

import { HRDrawer } from "@hackerrank/hrds-components";

import type { Brief, SessionInfo } from "@/lib/types";

import { BriefPanel } from "./BriefPanel";
import { PanelRightClose, PanelRightOpen } from "./icons";
import { Spinner } from "./ui";

/** The strip on the page's right edge that shows and hides the brief. */
export function BriefRail({
  open,
  pending,
  divider,
  onToggle,
}: {
  open: boolean;
  pending: boolean;
  divider: boolean;
  onToggle: () => void;
}) {
  return (
    <div className={`brief-rail ${divider ? "divider" : ""}`}>
      <button
        type="button"
        className="brief-rail-toggle"
        aria-expanded={open}
        aria-label={open ? "Hide the brief" : "Show the brief"}
        title={open ? "Hide the brief" : "Show the brief"}
        onClick={onToggle}
      >
        {open ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}
        <span className="brief-rail-label">Brief</span>
        {pending && <Spinner label="The brief is still being written" />}
      </button>
    </div>
  );
}

/** The brief inside its collapsible panel. A collapsed panel is 0 wide but still rendered, so `inert` keeps it out of the tab order. */
export function BriefPane({ brief, sessions, open }: { brief: Brief; sessions: SessionInfo[]; open: boolean }) {
  return (
    <div className="brief-pane-inner" inert={!open}>
      <div className="pane-head">
        <span className="pane-title">Brief</span>
      </div>
      <div className="pane-scroll">
        <BriefPanel brief={brief} sessions={sessions} />
      </div>
    </div>
  );
}

/** Narrow screens stack chat and diff, so the brief opens over them instead. */
export function BriefDrawer({
  brief,
  sessions,
  open,
  onClose,
}: {
  brief: Brief;
  sessions: SessionInfo[];
  open: boolean;
  onClose: () => void;
}) {
  return (
    <HRDrawer.Root isOpen={open} onClose={onClose} title="Brief" position="right" size="md">
      <HRDrawer.Body>
        <BriefPanel brief={brief} sessions={sessions} />
      </HRDrawer.Body>
    </HRDrawer.Root>
  );
}
