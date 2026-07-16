import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";
import type { Session } from "next-auth";

const auth = vi.fn((handler: unknown) => handler);
vi.mock("@/lib/auth", () => ({ auth }));

const { authorizeDashboardRequest } = await import("@/proxy");

function request(path: string) {
  return new NextRequest(`https://genus.example${path}`);
}

const verifiedSession = {
  user: { name: "Test Operator", email: "operator@example.com" },
  bridgeAccess: "signed-bridge-access-token",
  expires: "2099-01-01T00:00:00.000Z",
} satisfies Session;

describe("dashboard proxy authentication gate", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("keeps liveness and readiness public", () => {
    expect(authorizeDashboardRequest(request("/api/live"), null).status).toBe(200);
    expect(authorizeDashboardRequest(request("/api/ready"), null).status).toBe(200);
  });

  it("keeps the Cloudflare Access sign-in handler publicly reachable", () => {
    expect(
      authorizeDashboardRequest(request("/signin/cloudflare?callbackUrl=%2F"), null).status,
    ).toBe(200);
  });

  it("returns 401 for an unauthenticated private API request", async () => {
    const response = authorizeDashboardRequest(request("/api/bridge/people"), null);
    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({ error: "authentication required" });
  });

  it("redirects an unauthenticated page and preserves its callback", () => {
    const response = authorizeDashboardRequest(request("/tasks?view=mine"), null);
    expect(response.status).toBe(307);
    const location = new URL(response.headers.get("location")!);
    expect(location.pathname).toBe("/signin");
    expect(location.searchParams.get("callbackUrl")).toBe("/tasks?view=mine");
  });

  it("rejects an opaque cookie that Auth.js did not verify", async () => {
    const req = request("/tasks");
    req.cookies.set("authjs.session-token", "opaque");
    const response = authorizeDashboardRequest(req, null);
    expect(response.status).toBe(307);
  });

  it("requires a verified session and completed Bridge token exchange", async () => {
    expect(
      authorizeDashboardRequest(request("/api/chat/send"), {
        ...verifiedSession,
        bridgeAccess: undefined,
      }).status,
    ).toBe(401);
    expect(
      authorizeDashboardRequest(request("/api/chat/send"), verifiedSession).status,
    ).toBe(200);
    expect(
      authorizeDashboardRequest(request("/api/chat/send"), {
        ...verifiedSession,
        authError: "BridgeRefreshFailed",
      }).status,
    ).toBe(401);
  });

  it("allows only an explicit non-production insecure development mode", () => {
    vi.stubEnv("GENUS_INSECURE_DEV_MODE", "true");
    vi.stubEnv("GENUS_ENVIRONMENT", "development");
    expect(authorizeDashboardRequest(request("/tasks"), null).status).toBe(200);

    vi.stubEnv("GENUS_ENVIRONMENT", "production");
    expect(authorizeDashboardRequest(request("/tasks"), null).status).toBe(307);
  });
});
