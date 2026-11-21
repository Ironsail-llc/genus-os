/**
 * A room's outbox: ONE ordered structure, ONE sender.
 *
 * Every room message gets its `seq` when it is typed and enters the outbox.
 * Exactly one sender drains it, head first, one message per turn; a refusal
 * (`room_busy`) leaves the head where it is. Tested as a property: for
 * generated interleavings of typing, the server answering ok or busy, the
 * room freeing up, the retry wait elapsing and the reader navigating away
 * and back, the server receives the messages in typing order, each once.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ROOM_BUSY_RETRY_MS, forgetRoomOutboxes, roomOutbox, type OutboxItem, type SendResult } from "@/lib/chat/room-outbox";

const ROOM = "11111111-1111-4111-8111-111111111111";

/** A small seeded generator, so a failing interleaving can be replayed. */
function prng(seed: number) {
  let s = seed >>> 0 || 1;
  return () => {
    s ^= s << 13;
    s ^= s >>> 17;
    s ^= s << 5;
    return (s >>> 0) / 0x100000000;
  };
}

type Pending = { item: OutboxItem; resolve: (r: SendResult) => void };

/** The server: every send attempt waits here until the test answers it. */
function server() {
  const pending: Pending[] = [];
  const received: string[] = [];
  const attempts: string[] = [];
  let maxInFlight = 0;
  const sender = (item: OutboxItem) =>
    new Promise<SendResult>((resolve) => {
      attempts.push(item.text);
      pending.push({
        item,
        resolve: (r) => {
          if (r.outcome === "ok") received.push(item.text);
          resolve(r);
        },
      });
      maxInFlight = Math.max(maxInFlight, pending.length);
    });
  return { pending, received, attempts, sender, maxInFlight: () => maxInFlight };
}

const settle = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => {
  vi.useFakeTimers();
  forgetRoomOutboxes();
});
afterEach(() => {
  vi.useRealTimers();
  forgetRoomOutboxes();
});

describe("the room outbox", () => {
  it("gives each message its seq when it is typed, and keeps typing order", () => {
    const box = roomOutbox(ROOM);
    const a = box.enqueue("A");
    const b = box.enqueue("B");
    expect(b.seq).toBeGreaterThan(a.seq);
    expect(box.state().items.map((i) => i.text)).toEqual(["A", "B"]);
  });

  it("the reviewer's sequence: B typed while A's first send is in flight, A refused, B still goes after A", async () => {
    const srv = server();
    const box = roomOutbox(ROOM);
    box.attach(srv.sender);
    box.enqueue("A");
    await settle();
    box.enqueue("B");
    await settle();
    expect(srv.attempts).toEqual(["A"]);
    srv.pending.shift()!.resolve({ outcome: "busy", answering: "Alice" });
    await settle();
    expect(box.state()).toMatchObject({ blocked: true, answering: "Alice" });
    expect(box.state().items.map((i) => i.text)).toEqual(["A", "B"]);
    box.roomFreed();
    await settle();
    srv.pending.shift()!.resolve({ outcome: "ok" });
    await settle();
    srv.pending.shift()!.resolve({ outcome: "ok" });
    await settle();
    expect(srv.received).toEqual(["A", "B"]);
    expect(srv.attempts).toEqual(["A", "A", "B"]);
  });

  it("retries a refused head after the wait, without being told", async () => {
    const srv = server();
    const box = roomOutbox(ROOM);
    box.attach(srv.sender);
    box.enqueue("A");
    await settle();
    srv.pending.shift()!.resolve({ outcome: "busy", answering: "" });
    await settle();
    expect(srv.attempts).toEqual(["A"]);
    await vi.advanceTimersByTimeAsync(ROOM_BUSY_RETRY_MS);
    expect(srv.attempts).toEqual(["A", "A"]);
  });

  it("drops a message whose send failed outright, and carries on with the next", async () => {
    const srv = server();
    const box = roomOutbox(ROOM);
    box.attach(srv.sender);
    box.enqueue("A");
    box.enqueue("B");
    await settle();
    srv.pending.shift()!.resolve({ outcome: "failed" });
    await settle();
    expect(srv.attempts).toEqual(["A", "B"]);
  });

  it("outlives the panel: what is queued when the reader leaves goes out, in order, when they return", async () => {
    const srv = server();
    const box = roomOutbox(ROOM);
    const detach = box.attach(srv.sender);
    box.enqueue("A");
    await settle();
    srv.pending.shift()!.resolve({ outcome: "busy", answering: "Alice" });
    box.enqueue("B");
    await settle();
    detach();
    box.roomFreed();
    await vi.advanceTimersByTimeAsync(ROOM_BUSY_RETRY_MS);
    expect(srv.attempts).toEqual(["A"]);
    roomOutbox(ROOM).attach(srv.sender);
    await settle();
    srv.pending.shift()!.resolve({ outcome: "ok" });
    await settle();
    srv.pending.shift()!.resolve({ outcome: "ok" });
    await settle();
    expect(srv.received).toEqual(["A", "B"]);
  });

  it("property: for generated interleavings, the server receives every message in typing order, each once", async () => {
    const ACTIONS = ["type", "type", "ok", "ok", "busy", "freed", "elapse", "navigate"] as const;
    for (let seed = 1; seed <= 300; seed++) {
      forgetRoomOutboxes();
      const rand = prng(seed);
      const srv = server();
      const typed: string[] = [];
      let detach = roomOutbox(ROOM).attach(srv.sender);
      const trace: string[] = [];
      const steps = 10 + Math.floor(rand() * 40);
      for (let step = 0; step < steps; step++) {
        const action = ACTIONS[Math.floor(rand() * ACTIONS.length)];
        trace.push(action);
        if (action === "type") {
          const text = `m${typed.length}`;
          typed.push(text);
          roomOutbox(ROOM).enqueue(text);
        } else if (action === "ok" || action === "busy") {
          srv.pending.shift()?.resolve(action === "ok" ? { outcome: "ok" } : { outcome: "busy", answering: "Bob" });
        } else if (action === "freed") {
          roomOutbox(ROOM).roomFreed();
        } else if (action === "elapse") {
          await vi.advanceTimersByTimeAsync(ROOM_BUSY_RETRY_MS);
        } else {
          // Away (the request in flight carries on) and back: a new panel, a new sender.
          detach();
          await settle();
          detach = roomOutbox(ROOM).attach(srv.sender);
        }
        await settle();
        const context = `seed ${seed}: ${trace.join(" ")}`;
        // One sender: never two sends in flight.
        expect(srv.pending.length, context).toBeLessThanOrEqual(1);
        // What arrived is a prefix of what was typed, in order, nothing twice.
        expect(srv.received, context).toEqual(typed.slice(0, srv.received.length));
        // The one attempt in flight is always the oldest message not yet received.
        if (srv.pending[0]) expect(srv.pending[0].item.text, context).toBe(typed[srv.received.length]);
      }
      // Then the room frees up for good: everything arrives, in order, once.
      for (let guard = 0; guard < typed.length * 3 + 5 && srv.received.length < typed.length; guard++) {
        roomOutbox(ROOM).roomFreed();
        await settle();
        srv.pending.shift()?.resolve({ outcome: "ok" });
        await settle();
      }
      expect(srv.received, `seed ${seed}: ${trace.join(" ")}`).toEqual(typed);
      expect(srv.maxInFlight()).toBeLessThanOrEqual(1);
      detach();
    }
  });
});
