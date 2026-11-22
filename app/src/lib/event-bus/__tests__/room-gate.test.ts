/**
 * The events stream's second filter: after the tenant, the rooms this caller
 * can access. A `room.*` event for a room outside the caller's list never
 * leaves the server; the list is the bridge's (`GET /api/rooms`, the caller's
 * own token), cached per connection, refreshed once — and no more often than a
 * floor — when an unknown room appears. No list, no room events. Room events
 * carry ids only; anything else in the payload is not forwarded.
 */
import { describe, expect, it, vi } from "vitest";

import { createRoomGate } from "@/lib/event-bus/room-gate";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";

function ev(id: string, type: string, payload: Record<string, unknown>) {
  return { id, type, payload, tenant_id: "test-tenant" };
}

function clock(start = 0) {
  let t = start;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

describe("createRoomGate", () => {
  it("passes every other event untouched and never asks for the room list", async () => {
    const loadRoomIds = vi.fn(async () => new Set([A]));
    const gate = createRoomGate({ loadRoomIds });
    const events = [ev("1-0", "task.created", { title: "secret-ish", tenant_id: "x" })];
    expect(await gate.filter(events)).toEqual(events);
    expect(loadRoomIds).not.toHaveBeenCalled();
  });

  it("drops a room event for a room the caller cannot access", async () => {
    const gate = createRoomGate({ loadRoomIds: async () => new Set([A]) });
    const out = await gate.filter([
      ev("1-0", "room.message", { room_id: B, tenant_id: "test-tenant", message_id: 7 }),
      ev("2-0", "room.message", { room_id: A, tenant_id: "test-tenant", message_id: 8 }),
    ]);
    expect(out.map((e) => e.id)).toEqual(["2-0"]);
  });

  it("forwards ids only, never content", async () => {
    const gate = createRoomGate({ loadRoomIds: async () => new Set([A]) });
    const [out] = await gate.filter([
      ev("1-0", "room.edition", {
        room_id: A,
        tenant_id: "test-tenant",
        edition_id: "e-1",
        message_id: 3,
        task_id: "t-1",
        summary: "Revenue fell 40%",
        title: "Week 47",
      }),
    ]);
    expect(out.payload).toEqual({ room_id: A, tenant_id: "test-tenant", edition_id: "e-1", message_id: 3, task_id: "t-1" });
  });

  it("fails closed: no list, or a list that throws, means no room events", async () => {
    for (const loadRoomIds of [async () => null, async () => Promise.reject(new Error("down"))]) {
      const gate = createRoomGate({ loadRoomIds });
      const out = await gate.filter([
        ev("1-0", "room.message", { room_id: A, tenant_id: "test-tenant" }),
        ev("2-0", "task.created", {}),
      ]);
      expect(out.map((e) => e.id)).toEqual(["2-0"]);
    }
  });

  it("refreshes once for an unknown room, so a newly shared workspace is delivered", async () => {
    const lists = [new Set([A]), new Set([A, B])];
    const loadRoomIds = vi.fn(async () => lists.shift() ?? new Set([A, B]));
    const c = clock();
    const gate = createRoomGate({ loadRoomIds, now: c.now, minRefreshMs: 10_000 });
    await gate.filter([ev("1-0", "room.message", { room_id: A, tenant_id: "test-tenant" })]);
    c.advance(10_000);
    const out = await gate.filter([ev("2-0", "room.updated", { room_id: B, tenant_id: "test-tenant" })]);
    expect(out.map((e) => e.id)).toEqual(["2-0"]);
    expect(loadRoomIds).toHaveBeenCalledTimes(2);
  });

  it("bounds the refreshes a stream of unknown room ids can cause", async () => {
    const loadRoomIds = vi.fn(async () => new Set([A]));
    const c = clock();
    const gate = createRoomGate({ loadRoomIds, now: c.now, minRefreshMs: 10_000, ttlMs: 60_000 });
    for (let i = 0; i < 50; i++) {
      const id = `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
      expect(await gate.filter([ev(`${i}-0`, "room.message", { room_id: id, tenant_id: "test-tenant" })])).toEqual([]);
      c.advance(100);
    }
    // The first load, plus at most one refresh in the first ten seconds.
    expect(loadRoomIds.mock.calls.length).toBeLessThanOrEqual(2);
  });

  it("reads the list again once it is old, so revoked access stops events", async () => {
    const lists = [new Set([A]), new Set<string>()];
    const loadRoomIds = vi.fn(async () => lists.shift() ?? new Set<string>());
    const c = clock();
    const gate = createRoomGate({ loadRoomIds, now: c.now, ttlMs: 60_000 });
    expect(await gate.filter([ev("1-0", "room.message", { room_id: A, tenant_id: "test-tenant" })])).toHaveLength(1);
    c.advance(61_000);
    expect(await gate.filter([ev("2-0", "room.message", { room_id: A, tenant_id: "test-tenant" })])).toHaveLength(0);
  });

  it("admits a room the caller can open though it is not in their list (an admin, not a member)", async () => {
    const checkRoom = vi.fn(async (id: string) => id === B);
    const gate = createRoomGate({ loadRoomIds: async () => new Set([A]), checkRoom });
    const out = await gate.filter([ev("1-0", "room.message", { room_id: B, tenant_id: "test-tenant" })]);
    expect(out.map((e) => e.id)).toEqual(["1-0"]);
    // Remembered: the next event for it asks nothing.
    await gate.filter([ev("2-0", "room.message", { room_id: B, tenant_id: "test-tenant" })]);
    expect(checkRoom).toHaveBeenCalledTimes(1);
  });

  it("drops a room the caller cannot open, and one whose check fails, and remembers the refusal", async () => {
    const C = "33333333-3333-4333-8333-333333333333";
    const checkRoom = vi.fn(async (id: string) => {
      if (id === C) throw new Error("bridge down");
      return false;
    });
    const gate = createRoomGate({ loadRoomIds: async () => new Set([A]), checkRoom });
    expect(await gate.filter([ev("1-0", "room.message", { room_id: B, tenant_id: "test-tenant" })])).toEqual([]);
    expect(await gate.filter([ev("2-0", "room.message", { room_id: C, tenant_id: "test-tenant" })])).toEqual([]);
    await gate.filter([ev("3-0", "room.message", { room_id: B, tenant_id: "test-tenant" })]);
    expect(checkRoom.mock.calls.filter(([id]) => id === B)).toHaveLength(1);
  });

  it("never checks a value that is not a room id", async () => {
    const checkRoom = vi.fn(async () => true);
    const gate = createRoomGate({ loadRoomIds: async () => new Set([A]), checkRoom });
    expect(await gate.filter([ev("1-0", "room.message", { room_id: "../vault/get", tenant_id: "test-tenant" })])).toEqual([]);
    expect(checkRoom).not.toHaveBeenCalled();
  });

  it("bounds the single-room checks a stream of unknown ids can cause", async () => {
    const checkRoom = vi.fn(async () => false);
    const c = clock();
    const gate = createRoomGate({ loadRoomIds: async () => new Set([A]), checkRoom, now: c.now, minRefreshMs: 10_000 });
    for (let i = 0; i < 50; i++) {
      const id = `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
      await gate.filter([ev(`${i}-0`, "room.message", { room_id: id, tenant_id: "test-tenant" })]);
      c.advance(100);
    }
    expect(checkRoom.mock.calls.length).toBeLessThanOrEqual(5);
  });
});
