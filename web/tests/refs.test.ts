import { describe, expect, it } from "vitest";

import { linkifyRefs } from "@/components/Markdown";
import { firstHunkRef, isExcerptRef, parseRef, refLabel } from "@/lib/refs";

describe("parseRef", () => {
  it("parses hunk refs, keeping colons in the path", () => {
    expect(parseRef("hunk:src/greet.py:21")).toEqual({ kind: "hunk", raw: "hunk:src/greet.py:21", path: "src/greet.py", line: 21 });
    expect(parseRef("hunk:weird:name.py:3")).toMatchObject({ kind: "hunk", path: "weird:name.py", line: 3 });
  });

  it("parses file, session, and PR refs", () => {
    expect(parseRef("file:docs/notes.md")).toMatchObject({ kind: "file", path: "docs/notes.md" });
    expect(parseRef("claude:sess-1:uuid-2")).toMatchObject({ kind: "claude", session: "sess-1", id: "uuid-2" });
    expect(parseRef("codex:sess-9:turn-3")).toMatchObject({ kind: "codex", session: "sess-9", id: "turn-3" });
    expect(parseRef("pr:body")).toEqual({ kind: "pr", raw: "pr:body" });
  });

  it("marks malformed refs unknown", () => {
    for (const raw of ["", "nope", "hunk:src/a.py", "hunk:src/a.py:x", "hunk::4", "file:", "claude:only", "claude:s:", "pr:title", "zip:a:b"]) {
      expect(parseRef(raw).kind, raw).toBe("unknown");
    }
  });

  it("labels refs and finds the first hunk", () => {
    expect(refLabel(parseRef("hunk:src/greet.py:21"))).toBe("greet.py:21");
    expect(refLabel(parseRef("claude:11111111-aaaa:u"))).toBe("claude 11111111");
    expect(refLabel(parseRef("pr:body"))).toBe("PR description");
    expect(firstHunkRef(["pr:body", "hunk:a.py:3", "hunk:b.py:9"])).toMatchObject({ path: "a.py", line: 3 });
    expect(firstHunkRef(["pr:body"])).toBeNull();
    expect(isExcerptRef(parseRef("codex:s:t"))).toBe(true);
    expect(isExcerptRef(parseRef("file:a.py"))).toBe(false);
  });
});

describe("linkifyRefs", () => {
  it("wraps bare refs in backticks but leaves code alone", () => {
    expect(linkifyRefs("see claude:s1:u2 and pr:body.")).toBe("see `claude:s1:u2` and `pr:body`.");
    expect(linkifyRefs("already `claude:s1:u2`")).toBe("already `claude:s1:u2`");
    expect(linkifyRefs("```\nclaude:s1:u2\n```")).toBe("```\nclaude:s1:u2\n```");
    expect(linkifyRefs("hunk:src/a.py:12 changed")).toBe("`hunk:src/a.py:12` changed");
  });
});
