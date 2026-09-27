"use client";

import { useCallback, useEffect, useRef } from "react";

import { chatUrl } from "./api";
import { useReview } from "./store";
import type { Selection } from "./types";
import { ChatSocket } from "./ws";

/** Opens the pair agent chat for a review and feeds its events into the store. Pass null to stay disconnected. */
export function useChatSocket(reviewKey: string | null, onOpen?: () => void) {
  const dispatch = useReview((s) => s.dispatchChat);
  const sockRef = useRef<ChatSocket | null>(null);
  const onOpenRef = useRef(onOpen);
  useEffect(() => {
    onOpenRef.current = onOpen;
  });

  useEffect(() => {
    if (!reviewKey) return;
    const sock = new ChatSocket(chatUrl(reviewKey), {
      onEvent: (event) => dispatch({ type: "server", event }),
      onStatus: (status) => {
        dispatch({ type: "status", status });
        if (status === "open") onOpenRef.current?.();
      },
    });
    sockRef.current = sock;
    sock.connect();
    return () => {
      sock.close();
      if (sockRef.current === sock) sockRef.current = null;
    };
  }, [reviewKey, dispatch]);

  const send = useCallback(
    (text: string, selection: Selection | null) => {
      const ok = sockRef.current?.send({ type: "user_message", text, selection }) ?? false;
      if (ok) dispatch({ type: "sent", text, selection });
      return ok;
    },
    [dispatch],
  );

  const interrupt = useCallback(() => sockRef.current?.send({ type: "interrupt" }) ?? false, []);

  /** Stops reconnecting; used before closing the review so the socket doesn't restart the pair agent. */
  const close = useCallback(() => sockRef.current?.close(), []);

  return { send, interrupt, close };
}
