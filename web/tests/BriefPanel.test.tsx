import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BriefPanel } from "@/components/BriefPanel";
import { useReview } from "@/lib/store";
import type { Brief, ReviewMeta } from "@/lib/types";

import filled from "./fixtures/brief-filled.json";
import meta from "./fixtures/meta.json";
import pending from "./fixtures/brief-pending.json";
import { renderWithProviders } from "./render";

const COLLAPSIBLE = ["ask", "plan_vs_result", "decisions", "verification", "pushback", "side_effects", "churn", "loose_ends", "sessions"];

const section = (container: HTMLElement, name: string) => container.querySelector(`[data-section="${name}"]`) as HTMLElement;
const trigger = (container: HTMLElement, name: string) => section(container, name).querySelector(".section-trigger") as HTMLElement;

/** Opens a closed accordion section; its content isn't in the DOM until then. */
const expand = (container: HTMLElement, name: string) => {
  fireEvent.click(trigger(container, name));
  expect(trigger(container, name).getAttribute("data-state")).toBe("open");
  return section(container, name);
};

const texts = (el: HTMLElement) => [...el.querySelectorAll(".brief-item .text")].map((e) => e.textContent);

describe("BriefPanel", () => {
  it("shows placeholders for pending sections and the items that are ready", () => {
    const { container } = renderWithProviders(<BriefPanel brief={pending as Brief} />);
    for (const name of ["decisions", "pushback"]) {
      expect(trigger(container, name).querySelector(".spinner")).toBeTruthy();
      expect(trigger(container, name).querySelector(".section-count")).toBeNull();
    }
    expect(trigger(container, "ask").querySelector(".spinner")).toBeNull();

    for (const name of ["decisions", "pushback", "ask", "verification"]) expand(container, name);
    for (const name of ["summary", "look_first", "decisions", "pushback"]) {
      expect(screen.getByTestId(`pending-${name}`)).toBeTruthy();
    }
    expect(screen.queryByTestId("pending-ask")).toBeNull();
    expect(within(section(container, "ask")).getByText("Make the greeting optionally loud.")).toBeTruthy();
    expect(within(section(container, "verification")).getByText("passed")).toBeTruthy();
    expect(within(section(container, "verification")).getByText("2 runs · last", { exact: false })).toBeTruthy();
  });

  it("starts with every collapsible section closed", () => {
    const { container } = renderWithProviders(
      <BriefPanel brief={filled as Brief} sessions={(meta as ReviewMeta).sessions} />,
    );
    for (const name of COLLAPSIBLE) {
      expect(trigger(container, name).getAttribute("data-state")).toBe("closed");
      expect(section(container, name).querySelector(".brief-item")).toBeNull();
    }
    expect(section(container, "summary").textContent).toContain("Adds a loud flag to greet.");
    expect(within(section(container, "look_first")).getByText("The helper now multiplies instead of adding.")).toBeTruthy();
  });

  it("renders every section of a filled brief in order, with status tags and superseded decisions", () => {
    const { container } = renderWithProviders(
      <BriefPanel brief={filled as Brief} sessions={(meta as ReviewMeta).sessions} />,
    );
    expect(screen.queryByTestId(/^pending-/)).toBeNull();
    const order = [...container.querySelectorAll("[data-section]")].map((e) => e.getAttribute("data-section"));
    expect(order).toEqual([
      "summary",
      "look_first",
      "ask",
      "plan_vs_result",
      "decisions",
      "verification",
      "pushback",
      "side_effects",
      "churn",
      "loose_ends",
      "sessions",
    ]);

    const summary = section(container, "summary");
    expect(summary.querySelector("code")?.textContent).toBe("loud");

    const decisions = expand(container, "decisions");
    const reversed = decisions.querySelector(".brief-item.superseded") as HTMLElement;
    expect(within(reversed).getByText("Uppercase or exclamation?")).toBeTruthy();
    expect(within(reversed).getByText("reversed")).toBeTruthy();
    expect(within(reversed).getByText("reversed by", { exact: false })).toBeTruthy();
    expect(decisions.querySelectorAll(".brief-item.superseded")).toHaveLength(1);

    expect(within(expand(container, "verification")).getByText("never run")).toBeTruthy();
    expect(within(expand(container, "plan_vs_result")).getByText("not planned")).toBeTruthy();
    expect(within(expand(container, "pushback")).getByText("reversed a decision")).toBeTruthy();
    const sideEffects = section(container, "side_effects");
    expect(sideEffects.querySelector(".section-count")?.textContent).toBe("0");
    expect(sideEffects.querySelector("[data-state]")?.getAttribute("data-state")).toBe("closed");
    expect(within(expand(container, "loose_ends")).getByText("Empty names aren't handled.")).toBeTruthy();

    fireEvent.click(within(section(container, "sessions")).getByRole("button"));
    expect(screen.getByText("built the code")).toBeTruthy();
    expect(screen.getByText("discussion only")).toBeTruthy();
    expect(screen.getByText("opened the PR")).toBeTruthy();
  });

  it("doesn't claim a reversal when supersedes names no decision", () => {
    const stale = structuredClone(filled) as Brief;
    stale.sections.pushback![0].supersedes = ["claude:11111111-aaaa-bbbb-cccc-000000000001:u-1"];
    const { container } = renderWithProviders(<BriefPanel brief={stale} />);
    const pushback = expand(container, "pushback");
    expect(within(pushback).getByText("Use uppercase instead.")).toBeTruthy();
    expect(within(pushback).queryByText("reversed a decision")).toBeNull();
  });

  it("jumps to the first hunk of a look_first item and opens excerpts from chips", () => {
    const { container } = renderWithProviders(<BriefPanel brief={filled as Brief} />);
    fireEvent.click(within(section(container, "look_first")).getByText("The helper now multiplies instead of adding."));
    expect(useReview.getState().scrollRequest).toMatchObject({ path: "src/greet.py", line: 21, side: "additions" });

    fireEvent.click(within(section(container, "look_first")).getByText("PR description"));
    expect(useReview.getState().excerptRef).toBe("pr:body");

    fireEvent.click(within(expand(container, "ask")).getByText("claude 11111111"));
    expect(useReview.getState().excerptRef).toBe("claude:11111111-aaaa-bbbb-cccc-000000000001:u-1");
  });

  it("caps long sections, keeping reversed decisions", () => {
    const long = structuredClone(filled) as Brief;
    long.sections.decisions = Array.from({ length: 9 }, (_, i) => ({ text: `Decision ${i + 1}.`, refs: [], superseded: i === 8 }));
    long.sections.ask = Array.from({ length: 4 }, (_, i) => ({ text: `Ask ${i + 1}.`, refs: [] }));
    const { container } = renderWithProviders(<BriefPanel brief={long} />);

    const decisions = expand(container, "decisions");
    expect(decisions.querySelectorAll(".brief-item")).toHaveLength(6);
    expect(texts(decisions)).toEqual(["Decision 1.", "Decision 2.", "Decision 3.", "Decision 4.", "Decision 5.", "Decision 9."]);
    expect(within(decisions.querySelector(".brief-item.superseded") as HTMLElement).getByText("Decision 9.")).toBeTruthy();
    fireEvent.click(within(decisions).getByRole("button", { name: "Show 3 more" }));
    expect(decisions.querySelectorAll(".brief-item")).toHaveLength(9);
    fireEvent.click(within(decisions).getByRole("button", { name: "Show fewer" }));
    expect(decisions.querySelectorAll(".brief-item")).toHaveLength(6);

    const ask = expand(container, "ask");
    expect(texts(ask)).toEqual(["Ask 1.", "Ask 2.", "Ask 3."]);
    expect(within(ask).getByRole("button", { name: "Show 1 more" })).toBeTruthy();
  });

  it("doesn't show verification commands", () => {
    const { container } = renderWithProviders(<BriefPanel brief={filled as Brief} />);
    const verification = expand(container, "verification");
    expect(within(verification).getByText("pytest")).toBeTruthy();
    expect(verification.textContent).not.toContain("pytest -q");
  });

  it("clamps long text at three lines, or six for the summary", () => {
    const wordy = structuredClone(filled) as Brief;
    const text = "Make the greeting loud. ".repeat(10).trim();
    wordy.sections.summary = [{ text, refs: [] }];
    wordy.sections.ask = [{ text, refs: [] }];
    const { container } = renderWithProviders(<BriefPanel brief={wordy} />);
    expect(section(container, "summary").textContent).toContain(text);
    expect(section(container, "summary").querySelector(".more-toggle")).toBeNull();

    const ask = expand(container, "ask");
    fireEvent.click(within(ask).getByRole("button", { name: "Show more" }));
    expect(within(ask).getByRole("button", { name: "Show less" })).toBeTruthy();
  });
});
