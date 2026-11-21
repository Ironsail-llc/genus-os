/**
 * `room.*` events name their tenant twice: on the envelope and in the payload
 * (`{room_id, tenant_id, ...}`). A room event reaches the browser only when
 * BOTH are the session's tenant; one with no payload tenant is dropped, never
 * assumed to be the root's.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { visibleToTenant } from "@/lib/event-bus/tenant-filter";
import type { EventEnvelope } from "@/lib/event-bus/redis-client";

const ROOM = "11111111-1111-4111-8111-111111111111";

function event(id: string, type: string, tenant_id: string | undefined, payload: Record<string, unknown>): EventEnvelope {
  return {
    id,
    timestamp: "2026-11-21T00:00:00Z",
    type,
    source: "engine",
    actor: "main",
    payload,
    correlation_id: "",
    ...(tenant_id === undefined ? {} : { tenant_id }),
  };
}

describe("room events and the tenant filter", () => {
  beforeEach(() => {
    vi.stubEnv("ROBOTHOR_PLATFORM_TENANT", "");
    vi.stubEnv("ROBOTHOR_DEFAULT_TENANT", "");
  });
  afterEach(() => vi.unstubAllEnvs());

  it("passes a room event whose envelope and payload are both this tenant's", () => {
    const events = [event("1-0", "room.message", "test-tenant", { room_id: ROOM, tenant_id: "test-tenant" })];
    expect(visibleToTenant(events, "test-tenant").map((e) => e.id)).toEqual(["1-0"]);
  });

  it("drops a room event for another tenant, by envelope or by payload", () => {
    const events = [
      event("1-0", "room.edition", "other-tenant", { room_id: ROOM, tenant_id: "other-tenant" }),
      event("2-0", "room.message", "test-tenant", { room_id: ROOM, tenant_id: "other-tenant" }),
      event("3-0", "room.updated", undefined, { room_id: ROOM, tenant_id: "other-tenant" }),
    ];
    expect(visibleToTenant(events, "test-tenant")).toEqual([]);
  });

  it("drops a room event that carries no payload tenant at all", () => {
    const events = [
      event("1-0", "room.task", "default", { room_id: ROOM }),
      event("2-0", "room.message", undefined, { room_id: ROOM, tenant_id: "" }),
    ];
    expect(visibleToTenant(events, "default")).toEqual([]);
  });

  it("leaves every other event type to the envelope rule, as before", () => {
    const events = [
      event("1-0", "task.created", "test-tenant", {}),
      event("2-0", "task.created", "test-tenant", { tenant_id: "other-tenant" }),
      event("3-0", "roomy.thing", "test-tenant", {}),
    ];
    expect(visibleToTenant(events, "test-tenant").map((e) => e.id)).toEqual(["1-0", "2-0", "3-0"]);
  });
});
