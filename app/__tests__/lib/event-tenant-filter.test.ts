/**
 * Every engine cell and the bridge publish to the same Redis streams, tagged
 * with the tenant the event happened in. The engines read only their own
 * tenant's events; the dashboard must apply the same rule for the session it
 * serves, or a child tenant's admin sees the root's and every sibling's
 * events on the live-events page.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { eventTenant, visibleToTenant } from "@/lib/event-bus/tenant-filter";
import type { EventEnvelope } from "@/lib/event-bus/redis-client";

function event(id: string, tenant_id?: string): EventEnvelope {
  return {
    id,
    timestamp: "2026-10-30T00:00:00Z",
    type: "task.created",
    source: "bridge",
    actor: "alice",
    payload: { title: "t" },
    correlation_id: "",
    ...(tenant_id === undefined ? {} : { tenant_id }),
  };
}

describe("event tenant filtering", () => {
  beforeEach(() => {
    vi.stubEnv("ROBOTHOR_PLATFORM_TENANT", "");
    vi.stubEnv("ROBOTHOR_DEFAULT_TENANT", "");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("treats an untagged event as the root tenant's, as the engines do", () => {
    expect(eventTenant(event("1-0"))).toBe("default");
    expect(eventTenant(event("1-0", ""))).toBe("default");
    expect(eventTenant(event("1-0", "acme-child-a"))).toBe("acme-child-a");
  });

  it("follows the platform tenant setting for untagged events", () => {
    vi.stubEnv("ROBOTHOR_PLATFORM_TENANT", "acme-root");
    expect(eventTenant(event("1-0"))).toBe("acme-root");
  });

  it("shows a child session only that child's events", () => {
    const events = [event("1-0"), event("2-0", "acme-child-a"), event("3-0", "acme-child-b")];
    expect(visibleToTenant(events, "acme-child-a").map((e) => e.id)).toEqual(["2-0"]);
  });

  it("shows the root session the root's events and nothing of the children's", () => {
    const events = [event("1-0"), event("2-0", "default"), event("3-0", "acme-child-a")];
    expect(visibleToTenant(events, "default").map((e) => e.id)).toEqual(["1-0", "2-0"]);
  });

  it("shows nothing to a session with no tenant", () => {
    expect(visibleToTenant([event("1-0"), event("2-0", "acme-child-a")], undefined)).toEqual([]);
  });
});
