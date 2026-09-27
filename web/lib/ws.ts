import type { ClientEvent, ServerEvent } from "./types";

import type { ConnectionStatus } from "./chat";

/** Close codes the server uses for a refused Origin and an unknown review key; retrying won't help. */
export const FATAL_CLOSE_CODES = new Set([4403, 4404]);

export interface ChatSocketHandlers {
  onEvent(event: ServerEvent): void;
  onStatus(status: ConnectionStatus, detail?: string): void;
}

export interface ChatSocketOptions {
  WebSocketImpl?: typeof WebSocket;
  minDelayMs?: number;
  maxDelayMs?: number;
}

/** The pair agent chat socket. Reconnects with backoff; the server replays history on every connect. */
export class ChatSocket {
  private ws: WebSocket | null = null;
  private attempts = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private readonly Impl: typeof WebSocket;
  private readonly minDelay: number;
  private readonly maxDelay: number;

  constructor(
    private readonly url: string,
    private readonly handlers: ChatSocketHandlers,
    opts: ChatSocketOptions = {},
  ) {
    this.Impl = opts.WebSocketImpl ?? WebSocket;
    this.minDelay = opts.minDelayMs ?? 500;
    this.maxDelay = opts.maxDelayMs ?? 10_000;
  }

  connect(): void {
    this.stopped = false;
    this.open();
  }

  private open(): void {
    this.handlers.onStatus(this.attempts === 0 ? "connecting" : "reconnecting");
    const ws = new this.Impl(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.attempts = 0;
      this.handlers.onStatus("open");
    };
    ws.onmessage = (m) => {
      try {
        this.handlers.onEvent(JSON.parse(String(m.data)) as ServerEvent);
      } catch {
        // Ignore frames that aren't JSON.
      }
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.stopped) {
        this.handlers.onStatus("closed");
      } else if (FATAL_CLOSE_CODES.has(e.code)) {
        this.stopped = true;
        this.handlers.onStatus("refused", e.code === 4404 ? "no brief for this review" : "origin refused");
      } else {
        this.schedule();
      }
    };
  }

  /** Delay before the next attempt: doubles from minDelay up to maxDelay. */
  nextDelay(): number {
    return Math.min(this.maxDelay, this.minDelay * 2 ** Math.max(0, this.attempts - 1));
  }

  private schedule(): void {
    this.attempts += 1;
    this.handlers.onStatus("reconnecting");
    this.timer = setTimeout(() => {
      this.timer = null;
      if (!this.stopped) this.open();
    }, this.nextDelay());
  }

  send(event: ClientEvent): boolean {
    if (!this.ws || this.ws.readyState !== this.Impl.OPEN) return false;
    this.ws.send(JSON.stringify(event));
    return true;
  }

  close(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const ws = this.ws;
    this.ws = null;
    ws?.close();
    this.handlers.onStatus("closed");
  }
}
