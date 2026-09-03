import { beforeEach, describe, expect, it, vi } from "vitest";

import { GET as liveGET } from "@/app/api/live/route";
import { GET as readyGET } from "@/app/api/ready/route";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

describe("dashboard health contracts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    process.env.ROBOTHOR_ENGINE_URL = "http://engine:18800";
    process.env.BRIDGE_URL = "http://bridge:9100";
    process.env.ORCHESTRATOR_URL = "http://orchestrator:9099";
    process.env.AUTH_SECRET = "test-auth-secret";
    process.env.AUTH_OIDC_ISSUER = "https://idp.example.test";
    process.env.AUTH_OIDC_CLIENT_ID = "test-client";
    process.env.AUTH_OIDC_CLIENT_SECRET = "test-client-secret";
    process.env.GENUS_BRIDGE_SSO_SECRET = "test-bridge-secret";
  });

  it("keeps liveness independent of backends", async () => {
    const response = await liveGET();

    expect(response.status).toBe(200);
    expect((await response.json()).status).toBe("ok");
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns ready only when every required backend is ready", async () => {
    mockFetch.mockResolvedValue({ ok: true });

    const response = await readyGET();
    const body = await response.json();
    const urls = mockFetch.mock.calls.map((call: unknown[]) => String(call[0]));

    expect(response.status).toBe(200);
    expect(body.services.every((service: Record<string, unknown>) => !("url" in service))).toBe(
      true
    );
    expect(urls).toEqual(
      expect.arrayContaining([
        "http://engine:18800/ready",
        "http://bridge:9100/ready",
        "http://orchestrator:9099/ready",
      ])
    );
  });

  it("returns 503 when a required backend is unavailable", async () => {
    mockFetch
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ ok: false })
      .mockResolvedValueOnce({ ok: true });

    const response = await readyGET();

    expect(response.status).toBe(503);
    expect((await response.json()).status).toBe("degraded");
  });

  it("returns 503 when secure authentication is not configured", async () => {
    delete process.env.AUTH_SECRET;
    mockFetch.mockResolvedValue({ ok: true });

    const response = await readyGET();
    const body = await response.json();

    expect(response.status).toBe(503);
    expect(body.services).toContainEqual(
      expect.objectContaining({ name: "authentication", status: "unhealthy" }),
    );
  });
});

describe("authentication readiness knows both sign-in paths", () => {
  // 2026-08-27: this check demanded the OIDC triple unconditionally, so a box
  // authenticating exclusively through Cloudflare Access reported
  // `authentication: unhealthy` while signing users in perfectly well. A
  // permanent false `degraded` is what taught everyone to ignore /api/ready --
  // while the bridge was genuinely 403ing every sign-in.
  const clearAuthEnv = () => {
    for (const k of [
      "AUTH_OIDC_ISSUER",
      "AUTH_OIDC_CLIENT_ID",
      "AUTH_OIDC_CLIENT_SECRET",
      "CF_ACCESS_TEAM_DOMAIN",
      "CF_ACCESS_AUD",
      "GENUS_INSECURE_DEV_MODE",
    ]) {
      delete process.env[k];
    }
    process.env.AUTH_SECRET = "test-auth-secret";
    process.env.GENUS_BRIDGE_SSO_SECRET = "test-bridge-secret";
  };

  beforeEach(clearAuthEnv);

  it("is healthy on Cloudflare Access alone, with no OIDC at all", async () => {
    process.env.CF_ACCESS_TEAM_DOMAIN = "https://team.cloudflareaccess.com";
    process.env.CF_ACCESS_AUD = "aud-value";
    const { checkDashboardAuthConfig } = await import("@/lib/services/health");
    expect(checkDashboardAuthConfig().status).toBe("healthy");
  });

  it("is healthy on OIDC alone, with no Cloudflare Access", async () => {
    process.env.AUTH_OIDC_ISSUER = "https://idp.example.test";
    process.env.AUTH_OIDC_CLIENT_ID = "test-client";
    const { checkDashboardAuthConfig } = await import("@/lib/services/health");
    expect(checkDashboardAuthConfig().status).toBe("healthy");
  });

  it("is unhealthy when NEITHER provider is configured", async () => {
    const { checkDashboardAuthConfig } = await import("@/lib/services/health");
    expect(checkDashboardAuthConfig().status).toBe("unhealthy");
  });

  it("is unhealthy without the bridge SSO secret, whatever the provider", async () => {
    // The exact production failure: Cloudflare verified the user, then the
    // bridge exchange 403'd because it had no secret to compare against.
    process.env.CF_ACCESS_TEAM_DOMAIN = "https://team.cloudflareaccess.com";
    process.env.CF_ACCESS_AUD = "aud-value";
    delete process.env.GENUS_BRIDGE_SSO_SECRET;
    const { checkDashboardAuthConfig } = await import("@/lib/services/health");
    expect(checkDashboardAuthConfig().status).toBe("unhealthy");
  });

  it("keeps the local mirror of oidcProviderConfigured in sync with auth.ts", async () => {
    // health.ts deliberately does NOT import auth.ts (it builds the NextAuth
    // provider array at module scope, and a readiness probe must not drag
    // NextAuth in). This pins the copy so it cannot drift from the original.
    process.env.AUTH_OIDC_ISSUER = "https://idp.example.test";
    process.env.AUTH_OIDC_CLIENT_ID = "test-client";
    const { oidcProviderConfigured } = await import("@/lib/auth");
    const { checkDashboardAuthConfig } = await import("@/lib/services/health");
    expect(oidcProviderConfigured()).toBe(true);
    expect(checkDashboardAuthConfig().status).toBe("healthy");
  });
});
