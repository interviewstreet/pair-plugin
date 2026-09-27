import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatSocket } from "@/lib/ws";

class FakeSocket {
  static OPEN = 1;
  static all: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000 });
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  drop(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

const make = () => {
  const statuses: string[] = [];
  const events: unknown[] = [];
  const sock = new ChatSocket(
    "ws://x/chat",
    { onEvent: (e) => events.push(e), onStatus: (s) => statuses.push(s) },
    { WebSocketImpl: FakeSocket as unknown as typeof WebSocket, minDelayMs: 100, maxDelayMs: 400 },
  );
  return { sock, statuses, events };
};

describe("ChatSocket", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.all = [];
  });
  afterEach(() => vi.useRealTimers());

  it("reconnects with doubling delays and resets after a successful open", () => {
    const { sock, statuses } = make();
    sock.connect();
    FakeSocket.all[0].open();
    FakeSocket.all[0].drop();
    expect(statuses.at(-1)).toBe("reconnecting");
    vi.advanceTimersByTime(99);
    expect(FakeSocket.all).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all).toHaveLength(2);
    FakeSocket.all[1].drop();
    vi.advanceTimersByTime(199);
    expect(FakeSocket.all).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all).toHaveLength(3);
    FakeSocket.all[2].drop();
    vi.advanceTimersByTime(399);
    expect(FakeSocket.all).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all).toHaveLength(4);
    FakeSocket.all[3].open();
    expect(statuses.at(-1)).toBe("open");
    FakeSocket.all[3].drop();
    vi.advanceTimersByTime(100);
    expect(FakeSocket.all).toHaveLength(5);
  });

  it("parses events and sends only while open", () => {
    const { sock, events } = make();
    sock.connect();
    expect(sock.send({ type: "interrupt" })).toBe(false);
    FakeSocket.all[0].open();
    FakeSocket.all[0].onmessage?.({ data: JSON.stringify({ type: "history", messages: [], busy: false }) });
    FakeSocket.all[0].onmessage?.({ data: "not json" });
    expect(events).toEqual([{ type: "history", messages: [], busy: false }]);
    expect(sock.send({ type: "interrupt" })).toBe(true);
    expect(FakeSocket.all[0].sent).toEqual(['{"type":"interrupt"}']);
  });

  it("gives up on an unknown review and after close()", () => {
    const a = make();
    a.sock.connect();
    FakeSocket.all[0].drop(4404);
    vi.advanceTimersByTime(10_000);
    expect(FakeSocket.all).toHaveLength(1);
    expect(a.statuses.at(-1)).toBe("refused");

    const b = make();
    b.sock.connect();
    FakeSocket.all[1].open();
    b.sock.close();
    vi.advanceTimersByTime(10_000);
    expect(FakeSocket.all).toHaveLength(2);
    expect(b.statuses.at(-1)).toBe("closed");
  });
});
