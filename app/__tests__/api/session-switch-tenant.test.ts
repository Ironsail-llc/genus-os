/**
 * POST /api/session/switch-tenant — the only way the Helm changes the tenant a
 * session acts in. The bridge decides; this route passes the decision on and
 * never hands the acting bearer to the browser.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const switchSessionTenant = vi.fn();
vi.mock("@/lib/auth", () => ({ switchSessionTenant }));

const { POST } = await import("@/app/api/session/switch-tenant/route");

function request(body: unknown): NextRequest {
  return new NextRequest("http://localhost:3004/api/session/switch-tenant", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

describe("POST /api/session/switch-tenant", () => {
  beforeEach(() => {
    switchSessionTenant.mockReset();
  });

  it("switches and answers with the acting and home tenant only", async () => {
    switchSessionTenant.mockResolvedValue({
      ok: true,
      tenant: "acme-child-a",
      homeTenant: "acme-parent",
    });

    const res = await POST(request({ tenant: "acme-child-a" }));

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ tenant: "acme-child-a", homeTenant: "acme-parent" });
    expect(switchSessionTenant).toHaveBeenCalledWith("acme-child-a");
  });

  it("passes the bridge's refusal on with its status and words", async () => {
    switchSessionTenant.mockResolvedValue({
      ok: false,
      status: 403,
      error: "tenant switching disabled",
    });

    const res = await POST(request({ tenant: "acme-child-a" }));

    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ error: "tenant switching disabled" });
  });

  // 503 is the bridge refusing because row-level security is inert. Its body
  // carries operator fix text; the browser gets one distinct, fixed sentence.
  it("names an inert-isolation 503 distinctly instead of relaying the fix text", async () => {
    switchSessionTenant.mockResolvedValue({
      ok: false,
      status: 503,
      error: "tenant switching refused: row-level security is not enforcing. Set …",
    });

    const res = await POST(request({ tenant: "acme-child-a" }));

    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({
      error: "tenant switching unavailable: isolation not enforcing",
    });
  });

  it.each([
    ["no body", "not json"],
    ["no tenant", {}],
    ["an empty tenant", { tenant: "  " }],
    ["a non-string tenant", { tenant: 7 }],
    ["an over-long tenant", { tenant: "a".repeat(256) }],
  ])("refuses %s with 400 and never asks the bridge", async (_label, body) => {
    const res = await POST(request(body));

    expect(res.status).toBe(400);
    expect(switchSessionTenant).not.toHaveBeenCalled();
  });
});
