/**
 * The live-events stream serves one tenant: the session's acting tenant.
 * Events published by the root engine or a sibling cell never reach a child
 * admin's browser, and a request with no session gets nothing at all.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const session = vi.fn();
vi.mock("@/lib/auth", () => ({ auth: () => session() }));

const VALID = ["email", "calendar", "crm", "vision", "health", "agent", "system"];
vi.mock("@/lib/event-bus/redis-client", () => ({
  isValidStream: (s: string) => VALID.includes(s),
  readRecent: vi.fn().mockResolvedValue([
    { id: "3-0", timestamp: "", type: "task.created", source: "bridge", actor: "a", payload: { title: "child task" }, correlation_id: "", tenant_id: "acme-child-a" },
    { id: "2-0", timestamp: "", type: "task.created", source: "bridge", actor: "a", payload: { title: "sibling task" }, correlation_id: "", tenant_id: "acme-child-b" },
    { id: "1-0", timestamp: "", type: "ipc.interaction", source: "engine", actor: "a", payload: { contact_name: "Root Contact" }, correlation_id: "" },
  ]),
  // The long-poll loop ends on the first error, so the body closes.
  readSince: vi.fn().mockRejectedValue(new Error("closed")),
}));

import { GET } from "../route";

async function body(response: Response): Promise<string> {
  return new TextDecoder().decode(new Uint8Array(await response.arrayBuffer()));
}

describe("GET /api/events/stream", () => {
  beforeEach(() => {
    vi.stubEnv("ROBOTHOR_PLATFORM_TENANT", "");
    vi.stubEnv("ROBOTHOR_DEFAULT_TENANT", "");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    session.mockReset();
  });

  it("refuses a request with no session", async () => {
    session.mockResolvedValue(null);
    const response = await GET(new NextRequest("http://dashboard/api/events/stream?streams=crm"));
    expect(response.status).toBe(401);
  });

  it("gives a child session only its own tenant's events", async () => {
    session.mockResolvedValue({ bridgeAccess: "t", tenantId: "acme-child-a", homeTenantId: "acme-child-a" });
    const text = await body(await GET(new NextRequest("http://dashboard/api/events/stream?streams=crm")));
    expect(text).toContain("child task");
    expect(text).not.toContain("sibling task");
    expect(text).not.toContain("Root Contact");
  });

  it("gives the root session the root's events only", async () => {
    session.mockResolvedValue({ bridgeAccess: "t", tenantId: "default", homeTenantId: "default" });
    const text = await body(await GET(new NextRequest("http://dashboard/api/events/stream?streams=crm")));
    expect(text).toContain("Root Contact");
    expect(text).not.toContain("child task");
    expect(text).not.toContain("sibling task");
  });
});
