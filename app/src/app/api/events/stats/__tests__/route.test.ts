/**
 * Stream lengths are appliance-wide counts over every tenant's events, so
 * only a root session at home may read them.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const session = vi.fn();
vi.mock("@/lib/auth", () => ({ auth: () => session() }));
vi.mock("@/lib/event-bus/redis-client", () => ({
  streamLengths: vi.fn().mockResolvedValue({ crm: 3, agent: 2 }),
}));

import { GET } from "../route";

describe("GET /api/events/stats", () => {
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
    expect((await GET()).status).toBe(401);
  });

  it("refuses a child-tenant session", async () => {
    session.mockResolvedValue({ bridgeAccess: "t", tenantId: "acme-child-a", homeTenantId: "acme-child-a" });
    expect((await GET()).status).toBe(403);
  });

  it("refuses a root admin who is acting in a child", async () => {
    session.mockResolvedValue({ bridgeAccess: "t", tenantId: "acme-child-a", homeTenantId: "default" });
    expect((await GET()).status).toBe(403);
  });

  it("answers a root session at home", async () => {
    session.mockResolvedValue({ bridgeAccess: "t", tenantId: "default", homeTenantId: "default" });
    const response = await GET();
    expect(response.status).toBe(200);
    expect((await response.json()).total).toBe(5);
  });
});
