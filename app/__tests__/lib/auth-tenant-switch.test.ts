/**
 * Acting in another tenant, as the Auth.js session sees it.
 *
 * The bridge mints the acting token (`POST /api/auth/switch-tenant`); the
 * dashboard's only jobs are to keep it in the SAME session cookie, to leave
 * that cookie alone when the bridge refuses, and to survive a refresh — the
 * refresh token keeps the home tenant, so a refresh quietly hands back a home
 * token unless the session re-applies the switch.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "next-auth";
import type { JWT } from "next-auth/jwt";

import {
  bridgeJwtCallback,
  bridgeSessionCallback,
  publicBridgeSessionCallback,
  resetBridgeRefreshCacheForTests,
  withTenantSwitch,
} from "@/lib/auth";

const BRIDGE = "http://127.0.0.1:9100";

function homeToken(overrides: Partial<JWT> = {}): JWT {
  return {
    bridgeAccess: "home-access-token",
    bridgeRefresh: "home-refresh-token",
    accessExpiresAt: Date.now() + 10 * 60 * 1000,
    role: "owner",
    tenantId: "acme-parent",
    homeTenantId: "acme-parent",
    ...overrides,
  } as JWT;
}

function switched(tenant: string, access = `acting-${tenant}-token`) {
  return {
    access_token: access,
    refresh_token: null,
    user: {
      id: "user-1",
      email: "alice@example.com",
      display_name: "Alice",
      role: "owner",
      tenant_id: tenant,
      home_tenant_id: "acme-parent",
    },
  };
}

function reply(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

beforeEach(() => {
  vi.stubEnv("BRIDGE_URL", BRIDGE);
});

afterEach(() => {
  resetBridgeRefreshCacheForTests();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("switching the session's tenant", () => {
  it("asks the bridge with the current bearer and stores the acting token in the session", async () => {
    const fetchMock = vi.fn().mockResolvedValue(reply(200, switched("acme-child-a")));
    vi.stubGlobal("fetch", fetchMock);
    const token = homeToken();

    const outcome = await withTenantSwitch("acme-child-a", () =>
      bridgeJwtCallback({ token, trigger: "update" }),
    );

    expect(outcome).toEqual({ ok: true, tenant: "acme-child-a", homeTenant: "acme-parent" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BRIDGE}/api/auth/switch-tenant`);
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toBe("Bearer home-access-token");
    expect(JSON.parse(init.body)).toEqual({ tenant: "acme-child-a" });

    expect(token.bridgeAccess).toBe("acting-acme-child-a-token");
    // The refresh token is the home session's and is never replaced by a switch.
    expect(token.bridgeRefresh).toBe("home-refresh-token");
    expect(token.tenantId).toBe("acme-child-a");
    expect(token.homeTenantId).toBe("acme-parent");
    expect(token.actingTenant).toBe("acme-child-a");
  });

  it.each([
    [403, "tenant switching disabled"],
    [403, "tenant not accessible"],
    [503, "tenant switching refused: row-level security is not enforcing."],
  ])("leaves the session untouched when the bridge answers %s", async (status, error) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply(status, { error })));
    const token = homeToken();
    const before = { ...token };

    const outcome = await withTenantSwitch("acme-child-a", () =>
      bridgeJwtCallback({ token, trigger: "update" }),
    );

    expect(outcome).toEqual({ ok: false, status, error });
    expect(token).toEqual(before);
  });

  it("reports an unreachable bridge as a 502 and leaves the session untouched", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));
    const token = homeToken();
    const before = { ...token };

    const outcome = await withTenantSwitch("acme-child-a", () =>
      bridgeJwtCallback({ token, trigger: "update" }),
    );

    expect(outcome).toMatchObject({ ok: false, status: 502 });
    expect(token).toEqual(before);
  });

  it("switching to the home tenant restores it and stops acting", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reply(200, switched("acme-parent", "home-again"))));
    const token = homeToken({
      bridgeAccess: "acting-acme-child-a-token",
      tenantId: "acme-child-a",
      actingTenant: "acme-child-a",
    });

    const outcome = await withTenantSwitch("acme-parent", () =>
      bridgeJwtCallback({ token, trigger: "update" }),
    );

    expect(outcome).toEqual({ ok: true, tenant: "acme-parent", homeTenant: "acme-parent" });
    expect(token.bridgeAccess).toBe("home-again");
    expect(token.tenantId).toBe("acme-parent");
    expect(token.actingTenant).toBeUndefined();
  });

  it("refuses without calling the bridge when the session holds no credential", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const outcome = await withTenantSwitch("acme-child-a", () =>
      bridgeJwtCallback({ token: {} as JWT, trigger: "update" }),
    );

    expect(outcome).toMatchObject({ ok: false, status: 401 });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // `useSession().update(data)` reaches the same jwt callback from the
  // browser. A switch must only ever come from the BFF route, so an update
  // that names a tenant in its payload does nothing.
  it("ignores a browser-initiated session update that names a tenant", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const token = homeToken();

    await bridgeJwtCallback({
      token,
      trigger: "update",
      session: { switchTenant: "acme-child-a", actingTenant: "acme-child-a" },
    } as Parameters<typeof bridgeJwtCallback>[0]);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(token.tenantId).toBe("acme-parent");
    expect(token.actingTenant).toBeUndefined();
  });
});

describe("an acting session across an access-token refresh", () => {
  const refreshedHome = {
    access_token: "refreshed-home-token",
    refresh_token: "rotated-refresh-token",
    user: {
      id: "user-1",
      email: "alice@example.com",
      display_name: "Alice",
      role: "owner",
      tenant_id: "acme-parent",
    },
  };

  function expiredActing(): JWT {
    return homeToken({
      bridgeAccess: "acting-acme-child-a-token",
      tenantId: "acme-child-a",
      actingTenant: "acme-child-a",
      accessExpiresAt: 0,
    });
  }

  it("re-applies the acting tenant with the refreshed home token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(reply(200, refreshedHome))
      .mockResolvedValueOnce(reply(200, switched("acme-child-a", "re-acting-token")));
    vi.stubGlobal("fetch", fetchMock);

    const token = await bridgeJwtCallback({ token: expiredActing() });

    expect(fetchMock.mock.calls[1][0]).toBe(`${BRIDGE}/api/auth/switch-tenant`);
    expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe("Bearer refreshed-home-token");
    expect(token.bridgeAccess).toBe("re-acting-token");
    expect(token.bridgeRefresh).toBe("rotated-refresh-token");
    expect(token.tenantId).toBe("acme-child-a");
    expect(token.actingTenant).toBe("acme-child-a");
    expect(token.tenantNotice).toBeUndefined();
  });

  it("falls back to home and records a notice when the re-exchange is refused", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(reply(200, refreshedHome))
        .mockResolvedValueOnce(reply(403, { error: "tenant switching disabled" })),
    );

    const token = await bridgeJwtCallback({ token: expiredActing() });

    expect(token.bridgeAccess).toBe("refreshed-home-token");
    expect(token.tenantId).toBe("acme-parent");
    expect(token.actingTenant).toBeUndefined();
    expect(token.tenantNotice).toBe("acting-tenant-reverted");
  });

  it("a session acting nowhere makes no switch call on refresh", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(reply(200, refreshedHome));
    vi.stubGlobal("fetch", fetchMock);

    const token = await bridgeJwtCallback({ token: homeToken({ accessExpiresAt: 0 }) });

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(token.tenantId).toBe("acme-parent");
    expect(token.homeTenantId).toBe("acme-parent");
  });

  it("the notice is cleared once the browser acknowledges it", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const token = homeToken({ tenantNotice: "acting-tenant-reverted" });

    await bridgeJwtCallback({
      token,
      trigger: "update",
      session: { tenantNoticeSeen: true },
    } as Parameters<typeof bridgeJwtCallback>[0]);

    expect(token.tenantNotice).toBeUndefined();
  });
});

describe("what the session exposes about the acting tenant", () => {
  it("carries the acting and home tenant and the notice, never the bearer", async () => {
    const session = await publicBridgeSessionCallback({
      session: { user: { name: "Alice" }, expires: "2099-01-01T00:00:00.000Z" } as Session,
      token: homeToken({
        bridgeAccess: "acting-acme-child-a-token",
        tenantId: "acme-child-a",
        actingTenant: "acme-child-a",
        tenantNotice: "acting-tenant-reverted",
      }),
    });

    expect(session.tenantId).toBe("acme-child-a");
    expect(session.homeTenantId).toBe("acme-parent");
    expect(session.tenantNotice).toBe("acting-tenant-reverted");
    expect(JSON.stringify(session)).not.toContain("acting-acme-child-a-token");
  });

  it("server-side session keeps the bearer for the BFF", async () => {
    const session = await bridgeSessionCallback({
      session: { user: { name: "Alice" }, expires: "2099-01-01T00:00:00.000Z" } as Session,
      token: homeToken(),
    });

    expect(session.bridgeAccess).toBe("home-access-token");
    expect(session.homeTenantId).toBe("acme-parent");
  });
});
