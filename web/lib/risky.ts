import type { FileDiffMetadata } from "@pierre/diffs";

/** Same patterns as src/pair/risky.py. The server decides a hunk's tags; these only find the lines to mark. */
export const RISKY_PATTERNS: Record<string, RegExp> = {
  process: /\b(kill|pkill|killall|Kill(Session|Window|Pane|Server)|os\.Exit|Process\.kill|signal\.SIG(KILL|TERM))\b/,
  delete:
    /(rm -rf|os\.RemoveAll?\b|shutil\.rmtree|DROP (TABLE|COLUMN|INDEX)|DELETE FROM|TRUNCATE\b|\.destroy_all|\.delete_all|\bunlink\(|FileUtils\.rm)/i,
  exec: /(\bexec\.Command|\bsubprocess\.|\bos\.system|\bOpen3\.|`[^`\n]+`|\bsystem\(|\bchild_process|\bspawn\(|\bexecSync\()/,
  migration:
    /\b(ALTER TABLE|CREATE (UNIQUE )?INDEX|add_column|remove_column|create_table|drop_table|rename_column|change_column)\b/i,
  network:
    /(\bhttp\.(Get|Post|NewRequest)|\brequests\.(get|post|put|delete|request)|\burllib|\bfetch\(|Net::HTTP|Faraday|HTTParty|\baxios\b|\bhttpx\.)/,
};

/** Matches closer than this many lines share one marker. */
const MERGE_WINDOW = 5;

export interface RiskyMark {
  line: number;
  tags: string[];
}

/**
 * The added lines in the hunk starting at `hunkStart` (new side) that match the hunk's tags.
 * Tags no line matches (such as a migration path) go on the hunk's first line.
 */
export function riskyMarks(
  fd: Pick<FileDiffMetadata, "hunks" | "additionLines">,
  hunkStart: number,
  tags: string[],
): RiskyMark[] {
  const hunk = fd.hunks.find((h) => h.additionStart === hunkStart) ?? null;
  if (!hunk) return [{ line: hunkStart, tags }];
  const marks: RiskyMark[] = [];
  const matched = new Set<string>();
  let n = hunk.additionStart;
  for (const block of hunk.hunkContent) {
    if (block.type === "context") {
      n += block.lines;
      continue;
    }
    for (let k = 0; k < block.additions; k++) {
      const text = fd.additionLines[block.additionLineIndex + k] ?? "";
      const hits = tags.filter((t) => RISKY_PATTERNS[t]?.test(text));
      if (hits.length) {
        hits.forEach((t) => matched.add(t));
        const last = marks[marks.length - 1];
        if (last && n + k - last.line < MERGE_WINDOW) {
          for (const t of hits) if (!last.tags.includes(t)) last.tags.push(t);
        } else {
          marks.push({ line: n + k, tags: hits });
        }
      }
    }
    n += block.additions;
  }
  const rest = tags.filter((t) => !matched.has(t));
  if (rest.length) marks.unshift({ line: hunk.additionStart, tags: rest });
  return marks;
}
