import type { FileDiffMetadata, SelectedLineRange } from "@pierre/diffs";

import type { Selection, Side } from "./types";

/** The new-side line number shown next to an old-side line, so a range that crosses sides can be sent as one side. */
export function oldToNewLine(fileDiff: Pick<FileDiffMetadata, "hunks">, oldLine: number): number {
  let shift = 0;
  for (const h of fileDiff.hunks) {
    if (oldLine < h.deletionStart) break;
    if (oldLine >= h.deletionStart + h.deletionCount) {
      shift = h.additionStart + h.additionCount - (h.deletionStart + h.deletionCount);
      continue;
    }
    let o = h.deletionStart;
    let n = h.additionStart;
    for (const block of h.hunkContent) {
      if (block.type === "context") {
        if (oldLine < o + block.lines) return n + (oldLine - o);
        o += block.lines;
        n += block.lines;
      } else {
        if (oldLine < o + block.deletions) return n;
        o += block.deletions;
        n += block.additions;
      }
    }
    return n;
  }
  return oldLine + shift;
}

/**
 * Maps a CodeView line selection to the chat's selection payload. The item id is the file path.
 * A range that starts on one side and ends on the other is expressed on the additions side.
 */
export function selectionToPayload(
  id: string,
  range: SelectedLineRange,
  fileDiff?: Pick<FileDiffMetadata, "hunks">,
): Selection {
  const side: Side = range.side ?? "additions";
  const endSide: Side = range.endSide ?? side;
  let a = range.start;
  let b = range.end;
  let out: Side = side;
  if (side !== endSide) {
    out = "additions";
    if (side === "deletions") a = fileDiff ? oldToNewLine(fileDiff, a) : b;
    if (endSide === "deletions") b = fileDiff ? oldToNewLine(fileDiff, b) : a;
  }
  return { path: id, side: out, start: Math.min(a, b), end: Math.max(a, b) };
}

export function selectionLabel(s: Selection): string {
  const lines = s.start === s.end ? `line ${s.start}` : `lines ${s.start}-${s.end}`;
  return `${s.path} ${lines} (${s.side === "additions" ? "new" : "old"})`;
}
