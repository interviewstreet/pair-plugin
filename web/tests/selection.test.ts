import { readFileSync } from "node:fs";
import path from "node:path";

import { parsePatchFiles } from "@pierre/diffs";
import { describe, expect, it } from "vitest";

import { riskyMarks } from "@/lib/risky";
import { oldToNewLine, selectionLabel, selectionToPayload } from "@/lib/selection";

const patch = readFileSync(path.join(__dirname, "fixtures/sample.patch"), "utf8");
const files = parsePatchFiles(patch).flatMap((p) => p.files);
const greet = files.find((f) => f.name === "src/greet.py")!;

describe("oldToNewLine", () => {
  it("maps old lines through context, changes, and gaps", () => {
    expect(oldToNewLine(greet, 1)).toBe(1);
    expect(oldToNewLine(greet, 3)).toBe(3);
    expect(oldToNewLine(greet, 4)).toBe(3);
    expect(oldToNewLine(greet, 5)).toBe(6);
    expect(oldToNewLine(greet, 10)).toBe(11);
    expect(oldToNewLine(greet, 22)).toBe(23);
    expect(oldToNewLine(greet, 30)).toBe(31);
  });
});

describe("selectionToPayload", () => {
  it("keeps a single-side range and orders it", () => {
    expect(selectionToPayload("src/greet.py", { start: 5, end: 3, side: "additions" }, greet)).toEqual({
      path: "src/greet.py",
      side: "additions",
      start: 3,
      end: 5,
    });
    expect(selectionToPayload("src/greet.py", { start: 3, end: 4, side: "deletions", endSide: "deletions" }, greet)).toEqual({
      path: "src/greet.py",
      side: "deletions",
      start: 3,
      end: 4,
    });
  });

  it("defaults a missing side to additions", () => {
    expect(selectionToPayload("docs/notes.md", { start: 2, end: 2 })).toEqual({
      path: "docs/notes.md",
      side: "additions",
      start: 2,
      end: 2,
    });
  });

  it("maps a range that crosses sides onto the new side", () => {
    expect(selectionToPayload("src/greet.py", { start: 3, side: "deletions", end: 5, endSide: "additions" }, greet)).toEqual({
      path: "src/greet.py",
      side: "additions",
      start: 3,
      end: 5,
    });
    expect(selectionToPayload("src/greet.py", { start: 7, side: "additions", end: 22, endSide: "deletions" }, greet)).toEqual({
      path: "src/greet.py",
      side: "additions",
      start: 7,
      end: 23,
    });
  });

  it("labels the selection for the composer chip", () => {
    expect(selectionLabel({ path: "a.py", side: "deletions", start: 4, end: 4 })).toBe("a.py line 4 (old)");
    expect(selectionLabel({ path: "a.py", side: "additions", start: 4, end: 9 })).toBe("a.py lines 4-9 (new)");
  });
});

describe("riskyMarks", () => {
  it("marks the added lines that match the hunk's tags", () => {
    const risky = parsePatchFiles(
      [
        "diff --git a/run.go b/run.go",
        "--- a/run.go",
        "+++ b/run.go",
        "@@ -1,2 +1,6 @@",
        " package main",
        "+func stop(p *os.Process) {",
        "+\tp.Signal(signal.SIGTERM)",
        "+}",
        "+// cleanup",
        " ",
        "",
      ].join("\n"),
    ).flatMap((p) => p.files)[0];
    expect(riskyMarks(risky, 1, ["process"])).toEqual([{ line: 3, tags: ["process"] }]);
    expect(riskyMarks(risky, 1, ["migration"])).toEqual([{ line: 1, tags: ["migration"] }]);
    expect(riskyMarks(risky, 99, ["exec"])).toEqual([{ line: 99, tags: ["exec"] }]);
  });
});
