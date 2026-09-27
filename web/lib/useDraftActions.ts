"use client";

import { useCallback } from "react";

import { api } from "./api";
import { useReview } from "./store";
import type { DraftStatus } from "./types";
import { useNotify } from "./useNotify";

export function useDraftActions(reviewKey: string) {
  const upsertDraft = useReview((s) => s.upsertDraft);
  const notify = useNotify();

  const patch = useCallback(
    async (id: string, change: { body?: string; status?: DraftStatus }) => {
      try {
        upsertDraft(await api.patchDraft(reviewKey, id, change));
        return true;
      } catch (e) {
        notify("error", `Couldn't update the draft: ${(e as Error).message}`);
        return false;
      }
    },
    [reviewKey, upsertDraft, notify],
  );

  return { patch };
}
