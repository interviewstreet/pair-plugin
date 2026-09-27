"use client";

import { HRAccordion } from "@hackerrank/hrds-components";
import { useState } from "react";

import { type Brief, type BriefItem, SECTION_ORDER, type SessionInfo } from "@/lib/types";

import { BriefSectionItems, sectionTitle } from "./BriefSection";
import { SessionsList } from "./SessionsList";
import { Spinner } from "./ui";

const ALWAYS_OPEN = new Set(["summary", "look_first"]);

function Pending({ name }: { name: string }) {
  return (
    <div className="pending-row" data-testid={`pending-${name}`}>
      <Spinner />
      <span>Writing {sectionTitle(name).toLowerCase()}…</span>
    </div>
  );
}

function itemsOf(brief: Brief, name: string): BriefItem[] {
  const v = brief.sections?.[name];
  return Array.isArray(v) ? v : [];
}

export function BriefPanel({ brief, sessions }: { brief: Brief; sessions?: SessionInfo[] }) {
  const pending = new Set(brief.pending ?? []);
  const extra = Object.keys(brief.sections ?? {}).filter((n) => !(SECTION_ORDER as readonly string[]).includes(n));
  const names = [...SECTION_ORDER, ...extra];
  const collapsible = names.filter((n) => !ALWAYS_OPEN.has(n));
  const decisionRefs = new Set(itemsOf(brief, "decisions").flatMap((d) => (Array.isArray(d.refs) ? d.refs.slice(0, 1) : [])));
  const [open, setOpen] = useState<string[]>([]);

  return (
    <div className="brief">
      {[...ALWAYS_OPEN].map((n) => (
        <section key={n} className="brief-section" data-section={n}>
          <h3>{sectionTitle(n)}</h3>
          {pending.has(n) ? <Pending name={n} /> : <BriefSectionItems name={n} items={itemsOf(brief, n)} />}
        </section>
      ))}
      <HRAccordion.Root className="brief-accordion" type="multiple" value={open} onValueChange={setOpen}>
        {collapsible.map((n) => {
          const items = itemsOf(brief, n);
          return (
            <HRAccordion.Item key={n} value={n} data-section={n}>
              <HRAccordion.Trigger className="section-trigger">
                {sectionTitle(n)}
                {pending.has(n) ? <Spinner /> : <span className="section-count">{items.length}</span>}
              </HRAccordion.Trigger>
              <HRAccordion.Content className="section-content">
                {pending.has(n) ? (
                  <Pending name={n} />
                ) : (
                  <BriefSectionItems name={n} items={items} decisionRefs={decisionRefs} />
                )}
              </HRAccordion.Content>
            </HRAccordion.Item>
          );
        })}
        <HRAccordion.Item value="sessions" data-section="sessions">
          <HRAccordion.Trigger className="section-trigger">
            Sessions <span className="section-count">{(sessions ?? brief.sessions ?? []).length}</span>
          </HRAccordion.Trigger>
          <HRAccordion.Content className="section-content">
            <SessionsList sessions={sessions ?? brief.sessions ?? []} />
          </HRAccordion.Content>
        </HRAccordion.Item>
      </HRAccordion.Root>
    </div>
  );
}
