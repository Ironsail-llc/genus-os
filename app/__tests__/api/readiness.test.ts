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
