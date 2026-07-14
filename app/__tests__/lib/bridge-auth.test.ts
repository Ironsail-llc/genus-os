import { afterEach, describe, expect, it, vi } from "vitest";

const auth = vi.fn();
vi.mock("@/lib/auth", () => ({ auth }));
vi.mock("@/lib/config", () => ({ HELM_AGENT_ID: "helm-user" }));

const { bridgeAuthHeaders } = await import("@/lib/bridge-auth");

describe("bridgeAuthHeaders", () => {
  afterEach(() => {
    auth.mockReset();
    vi.unstubAllEnvs();
  });

  it("forwards a verified bridge session token", async () => {
    auth.mockResolvedValue({
      user: { email: "operator@example.com" },
      bridgeAccess: "signed-access-token",
    });
    await expect(bridgeAuthHeaders()).resolves.toEqual({
      Authorization: "Bearer signed-access-token",
    });
  });

  it("fails closed when there is no session", async () => {
    auth.mockResolvedValue(null);
    await expect(bridgeAuthHeaders()).resolves.toEqual({});
  });

  it("does not forward a stale token from an invalidated session", async () => {
    auth.mockResolvedValue({
      user: { email: "operator@example.com" },
      bridgeAccess: "stale-access-token",
      authError: "BridgeRefreshFailed",
    });
    await expect(bridgeAuthHeaders()).resolves.toEqual({});
  });

  it("allows a legacy agent header only in explicit non-production development", async () => {
    auth.mockResolvedValue(null);
    vi.stubEnv("GENUS_INSECURE_DEV_MODE", "true");
    vi.stubEnv("GENUS_ENVIRONMENT", "development");
    await expect(bridgeAuthHeaders()).resolves.toEqual({ "X-Agent-Id": "helm-user" });

    vi.stubEnv("GENUS_ENVIRONMENT", "production");
    await expect(bridgeAuthHeaders()).resolves.toEqual({});
  });
});
