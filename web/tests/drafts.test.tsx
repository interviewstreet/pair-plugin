import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DraftsFooter } from "@/components/DraftsDrawer";
import { NewComment } from "@/components/NewComment";
import { api } from "@/lib/api";
import { draftsMarkdown, footerAction, newDraftFor } from "@/lib/drafts";
import { useReview } from "@/lib/store";
import type { Draft } from "@/lib/types";

import { renderWithProviders } from "./render";

const base = { side: "additions" as const, created_at: "2026-01-02T11:00:00Z" };
const drafts: Draft[] = [
  { ...base, id: "a", path: "src/greet.py", line: 4, body: "Default `loud` to False?", status: "approved" },
  { ...base, id: "b", path: "src/greet.py", line: 23, start_line: 21, body: "Was * intended?", status: "draft" },
  { ...base, id: "c", path: "docs/notes.md", line: 1, body: "Typo.", status: "discarded" },
  { ...base, id: "d", path: "docs/notes.md", line: 3, body: "Posted already.", status: "posted" },
  { ...base, id: "e", path: "src/greet.py", side: "deletions", line: 3, body: "Old signature.", status: "draft" },
];

describe("footerAction", () => {
  it("offers Post with the approved count only when posting is allowed", () => {
    expect(footerAction(true, drafts, "acme-bot")).toEqual({ kind: "post", approved: 1, account: "acme-bot" });
    expect(footerAction(false, drafts, "acme-bot")).toEqual({ kind: "copy", count: 3 });
  });
});

describe("DraftsFooter", () => {
  it("shows only Post approved when can_post is true", () => {
    renderWithProviders(
      <DraftsFooter meta={{ can_post: true, post_account: "acme-bot" }} drafts={drafts} onPost={vi.fn()} onCopy={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Post approved (1) as acme-bot" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Copy all/ })).toBeNull();
  });

  it("shows only Copy all when can_post is false", () => {
    renderWithProviders(
      <DraftsFooter meta={{ can_post: false, post_account: null }} drafts={drafts} onPost={vi.fn()} onCopy={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Copy all (3)" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Post approved/ })).toBeNull();
  });
});

describe("writing a comment", () => {
  const target = { path: "src/greet.py", side: "additions" as const, start: 21, end: 23 };

  it("sends a range with start_line and a single line without it", () => {
    expect(newDraftFor(target, "  Why *?  ")).toEqual({
      path: "src/greet.py",
      side: "additions",
      line: 23,
      start_line: 21,
      body: "Why *?",
    });
    expect(newDraftFor({ ...target, side: "deletions", start: 3, end: 3 }, "Old.")).toMatchObject({
      side: "deletions",
      line: 3,
      start_line: null,
    });
  });

  it("saves the comment, adds it to the drafts, and closes the box", async () => {
    const saved: Draft = { ...base, id: "n1", path: "src/greet.py", line: 23, start_line: 21, body: "Why *?", status: "approved", author: "user" };
    const create = vi.spyOn(api, "createDraft").mockResolvedValue(saved);
    useReview.getState().setComposing(target);
    useReview.getState().setSelection(target);
    renderWithProviders(<NewComment reviewKey="acme__widgets__3" target={target} />);

    const add = screen.getByRole("button", { name: "Add comment" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("New comment"), { target: { value: "Why *?" } });
    fireEvent.click(add);

    await waitFor(() => expect(useReview.getState().drafts).toEqual([saved]));
    expect(create).toHaveBeenCalledWith("acme__widgets__3", newDraftFor(target, "Why *?"));
    expect(useReview.getState().composing).toBeNull();
    expect(useReview.getState().selection).toBeNull();
    create.mockRestore();
  });
});

describe("draftsMarkdown", () => {
  it("copies open drafts with locations, linking new-side lines to the head commit", () => {
    const md = draftsMarkdown(drafts, { repo: "acme/widgets" }, "abc123");
    expect(md).toContain("[`src/greet.py:4`](https://github.com/acme/widgets/blob/abc123/src/greet.py#L4)");
    expect(md).toContain("[`src/greet.py:21-23`](https://github.com/acme/widgets/blob/abc123/src/greet.py#L21-L23)");
    expect(md).toContain("`src/greet.py:3 (old)`");
    expect(md).not.toContain("Typo.");
    expect(md).not.toContain("Posted already.");
  });
});
