import type { Draft, NewDraft, PrInfo, Selection } from "./types";

/** The request for a comment written on `target`. A single line has no `start_line`. */
export function newDraftFor(target: Selection, body: string): NewDraft {
  return {
    path: target.path,
    side: target.side,
    line: target.end,
    start_line: target.start === target.end ? null : target.start,
    body: body.trim(),
  };
}

export type FooterAction =
  | { kind: "post"; approved: number; account: string | null }
  | { kind: "copy"; count: number };

/** Posting is offered only when the server allows it for this repo; everything else gets Copy all. */
export function footerAction(canPost: boolean, drafts: Draft[], account: string | null): FooterAction {
  if (canPost) return { kind: "post", approved: drafts.filter((d) => d.status === "approved").length, account };
  return { kind: "copy", count: copyable(drafts).length };
}

export function copyable(drafts: Draft[]): Draft[] {
  return drafts.filter((d) => d.status === "draft" || d.status === "approved");
}

export function draftLocation(d: Pick<Draft, "path" | "line" | "start_line" | "side">): string {
  const lines = d.start_line && d.start_line !== d.line ? `${d.start_line}-${d.line}` : `${d.line}`;
  return `${d.path}:${lines}${d.side === "deletions" ? " (old)" : ""}`;
}

/** Markdown for pasting the drafts into a GitHub review by hand. */
export function draftsMarkdown(drafts: Draft[], pr: Pick<PrInfo, "repo">, headSha: string): string {
  return copyable(drafts)
    .map((d) => {
      const first = d.start_line ?? d.line;
      const anchor = first === d.line ? `L${d.line}` : `L${first}-L${d.line}`;
      const where =
        d.side === "additions"
          ? `[\`${draftLocation(d)}\`](https://github.com/${pr.repo}/blob/${headSha}/${d.path}#${anchor})`
          : `\`${draftLocation(d)}\``;
      return `${where}\n\n${d.body.trim()}\n`;
    })
    .join("\n---\n\n");
}
