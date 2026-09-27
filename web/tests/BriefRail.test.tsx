import { fireEvent, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import { BriefRail } from "@/components/BriefPane";

import { renderWithProviders } from "./render";

const rail = (props: Partial<ComponentProps<typeof BriefRail>> = {}) =>
  renderWithProviders(<BriefRail open={false} pending={false} divider={false} onToggle={vi.fn()} {...props} />).container;

describe("BriefRail", () => {
  it("labels the toggle by whether the brief is open", () => {
    const closed = within(rail()).getByRole("button", { name: "Show the brief" });
    expect(closed.getAttribute("aria-expanded")).toBe("false");
    expect(within(closed).getByText("Brief")).toBeTruthy();

    const open = within(rail({ open: true })).getByRole("button", { name: "Hide the brief" });
    expect(open.getAttribute("aria-expanded")).toBe("true");
  });

  it("shows a spinner only while the brief is pending", () => {
    expect(within(rail()).queryByRole("status")).toBeNull();
    expect(within(rail({ pending: true })).getByRole("status", { name: "The brief is still being written" })).toBeTruthy();
  });

  it("adds the divider class only when asked", () => {
    expect(rail().querySelector(".brief-rail")?.classList.contains("divider")).toBe(false);
    expect(rail({ divider: true }).querySelector(".brief-rail")?.classList.contains("divider")).toBe(true);
  });

  it("calls onToggle when clicked", () => {
    const onToggle = vi.fn();
    rail({ onToggle });
    fireEvent.click(screen.getByRole("button", { name: "Show the brief" }));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});
