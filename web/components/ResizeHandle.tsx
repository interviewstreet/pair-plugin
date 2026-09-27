"use client";

import { PanelResizeHandle } from "react-resizable-panels";

export function ResizeHandle({
  direction = "horizontal",
  label,
}: {
  direction?: "horizontal" | "vertical";
  label?: string;
}) {
  return (
    <PanelResizeHandle
      className={`resize-handle ${direction}`}
      aria-label={label}
      // A wider hit area than the 1px divider makes the grip reachable without
      // widening the visible seam.
      hitAreaMargins={{ coarse: 16, fine: 6 }}
    >
      <span className="grip" aria-hidden="true" />
    </PanelResizeHandle>
  );
}
