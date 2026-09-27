import { describe, expect, it } from "vitest";

import { shortPath, toTranscript } from "@/lib/transcript";
import type { ExcerptMessage } from "@/lib/types";

const m = (role: string, text: string, focus = false): ExcerptMessage => ({ role, text, focus });

describe("toTranscript", () => {
  it("groups Claude messages by speaker and attaches Bash output to its call", () => {
    const blocks = toTranscript([
      m("user", "add rate limiting"),
      m("assistant", "Looking at the login route."),
      m("tool", "Bash: rg login src"),
      m("tool", "→ src/login.py:3\nsrc/login.py:9", true),
      m("tool", "Edit /home/a/repo/src/login.py: -'x = 1' +'x = 2\\ny = 3'"),
      m("user", "[Request interrupted by user]"),
    ]);
    expect(blocks.map((b) => b.speaker)).toEqual(["human", "agent", "human"]);
    const agent = blocks[1];
    if (agent.speaker !== "agent") throw new Error("expected the agent");
    expect(agent.entries.map((e) => e.kind)).toEqual(["text", "tool", "tool"]);
    expect(agent.entries[1]).toMatchObject({ name: "Bash", arg: "rg login src", output: "src/login.py:3\nsrc/login.py:9", focus: true });
    expect(agent.entries[2]).toMatchObject({
      name: "Edit",
      arg: "/home/a/repo/src/login.py",
      diff: [
        { sign: "-", text: "x = 1" },
        { sign: "+", text: "x = 2" },
        { sign: "+", text: "y = 3" },
      ],
    });
    const last = blocks[2];
    expect(last.speaker === "human" && last.entries[0].kind).toBe("interrupt");
  });

  it("reads Codex's combined command output and unified diffs", () => {
    const blocks = toTranscript([
      m("user", "fix it"),
      m("tool", "Bash: pytest -q\n→ 3 passed"),
      m("tool", "Edit /r/app.py:\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new"),
      m("tool", "Edit /r/gone.rb:\n"),
    ]);
    const agent = blocks[1];
    if (agent.speaker !== "agent") throw new Error("expected the agent");
    expect(agent.entries[0]).toMatchObject({ name: "Bash", arg: "pytest -q", output: "3 passed" });
    expect(agent.entries[1]).toMatchObject({
      name: "Edit",
      arg: "/r/app.py",
      diff: [
        { sign: "@", text: "@@ -1 +1 @@" },
        { sign: "-", text: "old" },
        { sign: "+", text: "new" },
      ],
    });
    expect(agent.entries[2]).toMatchObject({ name: "Edit", arg: "/r/gone.rb", diff: null });
  });

  it("shows a question as the agent's and its answer as the human's", () => {
    const blocks = toTranscript([
      m("tool", 'AskUserQuestion: {"questions": []}'),
      m("tool", "Asked: Keep the old route?\nAnswer: No"),
      m("tool", "Asked: Add a flag?\nAnswer: None"),
    ]);
    expect(blocks).toEqual([
      { speaker: "agent", entries: [{ kind: "question", text: "Keep the old route?", focus: false }] },
      { speaker: "human", entries: [{ kind: "answer", text: "No", focus: false }] },
      { speaker: "agent", entries: [{ kind: "question", text: "Add a flag?", focus: false }] },
      { speaker: "human", entries: [{ kind: "answer", text: "", focus: false }] },
    ]);
  });

  it("marks where messages were left out", () => {
    const blocks = toTranscript([m("user", "go"), m("assistant", "done")], 4, 2);
    expect(blocks.map((b) => b.speaker)).toEqual(["human", "gap", "agent", "gap"]);
    expect(blocks[1]).toEqual({ speaker: "gap", count: 4, after: false });
    expect(blocks[3]).toEqual({ speaker: "gap", count: 2, after: true });
  });
});

describe("shortPath", () => {
  it("names a path as the PR does, else shortens the home folder", () => {
    expect(shortPath("/Users/k/dev/repo/src/app.py", ["src/app.py"])).toBe("src/app.py");
    expect(shortPath("/Users/k/notes.md", [])).toBe("~/notes.md");
    expect(shortPath("src/app.py", [])).toBe("src/app.py");
  });
});
