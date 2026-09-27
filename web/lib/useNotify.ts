"use client";

import { useToast } from "@hackerrank/hrds-components";
import { type ReactNode, useCallback, useEffect, useRef } from "react";

type Kind = "basic" | "success" | "warning" | "error";

/**
 * A stable toast function. HRDS's `notify` changes identity on every render, which would
 * otherwise rebuild CodeView options and cancel in-flight scrolls.
 */
export function useNotify() {
  const { notify } = useToast();
  const ref = useRef(notify);
  useEffect(() => {
    ref.current = notify;
  });
  return useCallback(
    (type: Kind, message: ReactNode, duration?: number) => ref.current({ type, placement: "topRight", message, duration }),
    [],
  );
}
