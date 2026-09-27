"use client";

import type { MouseEvent } from "react";

import { parseRef, refLabel } from "@/lib/refs";
import { useReview } from "@/lib/store";

export function RefChip({ value, label }: { value: string; label?: string }) {
  const scrollTo = useReview((s) => s.scrollTo);
  const openExcerpt = useReview((s) => s.openExcerpt);
  const ref = parseRef(value);

  if (ref.kind === "unknown") {
    return (
      <span className="ref-chip inert" title={value}>
        {label ?? value}
      </span>
    );
  }

  const onClick = (e: MouseEvent) => {
    e.stopPropagation();
    if (ref.kind === "hunk") scrollTo(ref.path, ref.line, "additions");
    else if (ref.kind === "file") scrollTo(ref.path);
    else openExcerpt(ref.raw);
  };

  return (
    <button type="button" className="ref-chip" title={value} onClick={onClick}>
      {label ?? refLabel(ref)}
    </button>
  );
}
