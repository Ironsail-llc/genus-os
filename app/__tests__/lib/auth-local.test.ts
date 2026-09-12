import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JWT } from "next-auth/jwt";

const loginResult = {
  access_token: "bridge-access-token",
  refresh_token: "bridge-refresh-token",
  mfa_setup_required: true,
  user: {
    id: "user-1",
    email: "alice@example.com",
    display_name: "Alice",
    role: "owner",
    tenant_id: "default",
  },
};

function response(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

async function localProvider() {
  const { authConfig } = await import("@/lib/auth");
  return authConfig.providers.find((provider) => {
    const p = provider as { id?: string; options?: { id?: string } };
    return (p.options?.id ?? p.id) === "local";
  }) as
    | {
        options?: {
          id?: string;
          name?: string;
          authorize?: (
            credentials: Record<string, unknown>,
            request?: Request,
          ) => Promise<unknown>;
        };
        id?: string;
        name?: string;
        authorize?: (
          credentials: Record<string, unknown>,
          request?: Request,
        ) => Promise<unknown>;
      }
    | undefined;
}

function authorizeOf(provider: Awaited<ReturnType<typeof localProvider>>) {
  return (provider?.options?.authorize ?? provider?.authorize)!;
}

describe("local credentials provider registration", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("is absent unless GENUS_LOCAL_LOGIN is set to something the bridge reads as true", async () => {
    for (const value of ["", "false", "off", "no", "0", "enabled", "sure"]) {
      vi.resetModules();
      vi.stubEnv("GENUS_LOCAL_LOGIN", value);
      expect(await localProvider()).toBeUndefined();
    }
  });

  // The bridge parses this field with pydantic, which reads all of these as
  // true. Requiring exactly "true" here meant GENUS_LOCAL_LOGIN=1 published the
  // public password endpoint with no form in front of it: the surface without
  // the UI, which is the wrong half to fail open.
  it("registers for every spelling the bridge accepts", async () => {
    for (const value of ["true", "TRUE", "1", "yes", "on", "t", "y", " True "]) {
      vi.resetModules();
      vi.stubEnv("GENUS_LOCAL_LOGIN", value);
      expect(await localProvider(), value).toBeDefined();
    }
  });

  it("registers alongside the SSO providers when enabled", async () => {
    vi.resetModules();
    vi.stubEnv("GENUS_LOCAL_LOGIN", "true");
    vi.stubEnv("AUTH_OIDC_ISSUER", "https://idp.example.test");
    vi.stubEnv("AUTH_OIDC_CLIENT_ID", "client-1");
    const { authConfig } = await import("@/lib/auth");
    const ids = authConfig.providers.map((provider) => {
      const p = provider as { id?: string; options?: { id?: string } };
      return p.options?.id ?? p.id ?? "";
    });
    expect(ids).toContain("local");
    expect(ids).toContain("oidc");
  });

  it("is named for the human reading the sign-in page", async () => {
    vi.resetModules();
    vi.stubEnv("GENUS_LOCAL_LOGIN", "true");
    const provider = await localProvider();
    expect(provider?.options?.name ?? provider?.name).toBe("Email and password");
  });
});

describe("local authorize()", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("GENUS_LOCAL_LOGIN", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("posts the credentials to the bridge and returns the issued tokens", async () => {
    const fetchMock = vi.fn(async () => response(200, loginResult));
    vi.stubGlobal("fetch", fetchMock);
    const provider = await localProvider();
    const user = (await authorizeOf(provider)({
      email: "alice@example.com",
      password: "correct horse battery staple",
      code: "123456",
    })) as { localTokens?: typeof loginResult; email?: string };

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/auth/login");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      email: "alice@example.com",
      password: "correct horse battery staple",
      code: "123456",
    });
    expect(user?.email).toBe("alice@example.com");
    expect(user?.localTokens?.access_token).toBe("bridge-access-token");
  });

  it("omits an empty code rather than sending a blank one", async () => {
    const fetchMock = vi.fn(async () => response(200, loginResult));
    vi.stubGlobal("fetch", fetchMock);
    const provider = await localProvider();
    await authorizeOf(provider)({ email: "alice@example.com", password: "x".repeat(12), code: "" });
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      email: "alice@example.com",
      password: "x".repeat(12),
    });
  });

  it("throws a signin error whose code is mfa_required", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(401, { error: "mfa_required" })));
    const provider = await localProvider();
    await expect(
      authorizeOf(provider)({ email: "alice@example.com", password: "x".repeat(12) }),
    ).rejects.toMatchObject({ code: "mfa_required" });
  });

  it("returns null on a generic failure so Auth.js reports CredentialsSignin", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(401, { error: "invalid credentials" })));
    const provider = await localProvider();
    await expect(
      authorizeOf(provider)({ email: "alice@example.com", password: "x".repeat(12) }),
    ).resolves.toBeNull();
  });

  it("returns null when the bridge throttles or is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(429, { error: "too many attempts" })));
    const provider = await localProvider();
    await expect(
      authorizeOf(provider)({ email: "alice@example.com", password: "x".repeat(12) }),
    ).resolves.toBeNull();

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("ECONNREFUSED");
      }),
    );
    await expect(
      authorizeOf(provider)({ email: "alice@example.com", password: "x".repeat(12) }),
    ).resolves.toBeNull();
  });

  it("rejects a malformed bridge response instead of half-creating a session", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(200, { access_token: "a" })));
    const provider = await localProvider();
    await expect(
      authorizeOf(provider)({ email: "alice@example.com", password: "x".repeat(12) }),
    ).resolves.toBeNull();
  });
});

describe("local branch of the session callbacks", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("GENUS_LOCAL_LOGIN", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("allows a local sign-in carrying bridge tokens, and refuses one without", async () => {
    const { signInAllowed } = await import("@/lib/auth");
    expect(
      signInAllowed({
        account: { provider: "local" },
        user: { id: "user-1", localTokens: loginResult },
      }),
    ).toBe(true);
    expect(signInAllowed({ account: { provider: "local" }, user: { id: "user-1" } })).toBe(false);
  });

  it("applies the tokens directly, with no second SSO exchange", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { bridgeJwtCallback } = await import("@/lib/auth");
    const token = await bridgeJwtCallback({
      token: {} as JWT,
      account: { provider: "local" },
      user: { id: "user-1", localTokens: loginResult },
      trigger: "signIn",
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(token.bridgeAccess).toBe("bridge-access-token");
    expect(token.bridgeRefresh).toBe("bridge-refresh-token");
    expect(token.role).toBe("owner");
    expect(token.tenantId).toBe("default");
    expect(token.mfaSetupRequired).toBe(true);
    expect(token.bridgeAuthError).toBeUndefined();
  });

  it("aborts a local sign-in that arrives without tokens", async () => {
    const { bridgeJwtCallback } = await import("@/lib/auth");
    await expect(
      bridgeJwtCallback({
        token: {} as JWT,
        account: { provider: "local" },
        user: { id: "user-1" },
        trigger: "signIn",
      }),
    ).rejects.toThrow();
  });

  it("never serializes the bridge credential into the browser session", async () => {
    const { publicBridgeSessionCallback } = await import("@/lib/auth");
    const session = await publicBridgeSessionCallback({
      session: { user: { email: "alice@example.com" }, expires: "" } as never,
      token: {
        bridgeAccess: "bridge-access-token",
        bridgeRefresh: "bridge-refresh-token",
        role: "owner",
        tenantId: "default",
        mfaSetupRequired: true,
      } as JWT,
    });
    expect(session.bridgeAccess).toBeUndefined();
    expect(JSON.stringify(session)).not.toContain("bridge-refresh-token");
    expect(session.mfaSetupRequired).toBe(true);
  });
});

describe("forwarding the browser's address to the bridge", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("GENUS_LOCAL_LOGIN", "true");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  function requestWith(headers: Record<string, string>) {
    return { headers: new Headers(headers) } as unknown as Request;
  }

  async function headerFor(headers: Record<string, string>) {
    // authorize() runs SERVER-side, so without a forwarded address the bridge
    // sees the dashboard for every sign-in on the planet and its per-IP limiter
    // is one global bucket - five attempts from anywhere lock everyone out.
    const fetchMock = vi.fn(async () => response(200, loginResult));
    vi.stubGlobal("fetch", fetchMock);
    const provider = await localProvider();
    await authorizeOf(provider)(
      { email: "alice@example.com", password: "x".repeat(12) },
      requestWith(headers),
    );
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    return (init.headers as Record<string, string>)["X-Client-IP"];
  }

  it("sends nothing at all when no proxy is trusted", async () => {
    // The fail-safe default: a deployment that has not declared its edge cannot
    // vouch for an address, and the bridge then uses its own peer address.
    expect(await headerFor({ "x-forwarded-for": "203.0.113.7, 70.41.3.18" })).toBeUndefined();
  });

  it("takes the right-most untrusted hop, not the client-writable left-most one", async () => {
    // `proxy_add_x_forwarded_for` (nginx, ingress-nginx, Cloudflare) APPENDS,
    // so the list reads `<whatever the client sent>, <the real client>`. Taking
    // [0] let an attacker choose the limiter key and the audit subject.
    vi.stubEnv("GENUS_DASHBOARD_TRUSTED_PROXIES", "70.41.3.0/24");
    expect(await headerFor({ "x-forwarded-for": "1.2.3.4, 203.0.113.7, 70.41.3.18" })).toBe(
      "203.0.113.7",
    );
  });

  it("ignores an address the client injected on the left", async () => {
    vi.stubEnv("GENUS_DASHBOARD_TRUSTED_PROXIES", "70.41.3.18");
    expect(await headerFor({ "x-forwarded-for": "198.51.100.66, 70.41.3.18" })).toBe(
      "198.51.100.66",
    );
    // ...and the forged entry is not what is sent.
    expect(await headerFor({ "x-forwarded-for": "198.51.100.66, 70.41.3.18" })).not.toBe(
      "198.51.100.66, 70.41.3.18",
    );
  });

  it("sends nothing when every hop in the list is a trusted proxy", async () => {
    vi.stubEnv("GENUS_DASHBOARD_TRUSTED_PROXIES", "70.41.3.0/24");
    expect(await headerFor({ "x-forwarded-for": "70.41.3.9, 70.41.3.18" })).toBeUndefined();
  });

  it("falls back to x-real-ip only when a proxy is trusted", async () => {
    expect(await headerFor({ "x-real-ip": "198.51.100.9" })).toBeUndefined();
    vi.stubEnv("GENUS_DASHBOARD_TRUSTED_PROXIES", "70.41.3.18");
    expect(await headerFor({ "x-real-ip": "198.51.100.9" })).toBe("198.51.100.9");
    expect(await headerFor({})).toBeUndefined();
  });

  it("never forwards a header value that is not an address", async () => {
    vi.stubEnv("GENUS_DASHBOARD_TRUSTED_PROXIES", "70.41.3.18");
    expect(await headerFor({ "x-forwarded-for": "not an address" })).toBeUndefined();
    expect(await headerFor({ "x-forwarded-for": "1.2.3.4, junk, 70.41.3.18" })).toBeUndefined();
  });
});

describe("the dashboard's trusted-proxy allowlist", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("matches addresses and CIDR ranges, v4 and v6", async () => {
    const { isTrustedProxy } = await import("@/lib/auth-local");
    expect(isTrustedProxy("70.41.3.18", ["70.41.3.18"])).toBe(true);
    expect(isTrustedProxy("70.41.3.19", ["70.41.3.18"])).toBe(false);
    expect(isTrustedProxy("10.42.7.9", ["10.42.0.0/16"])).toBe(true);
    expect(isTrustedProxy("10.43.7.9", ["10.42.0.0/16"])).toBe(false);
    expect(isTrustedProxy("127.0.0.1", ["127.0.0.1/32"])).toBe(true);
    expect(isTrustedProxy("2001:db8::1", ["2001:db8::/32"])).toBe(true);
    expect(isTrustedProxy("2001:dba::1", ["2001:db8::/32"])).toBe(false);
    expect(isTrustedProxy("::1", ["::1"])).toBe(true);
    // Families do not cross.
    expect(isTrustedProxy("10.42.7.9", ["2001:db8::/32"])).toBe(false);
  });

  it("refuses a malformed entry rather than matching everything", async () => {
    const { isTrustedProxy } = await import("@/lib/auth-local");
    // An empty or non-numeric prefix must never read as /0.
    for (const entry of ["10.0.0.0/", "10.0.0.0/abc", "10.0.0.0/999", "", "nonsense"]) {
      expect(isTrustedProxy("198.51.100.7", [entry]), entry).toBe(false);
    }
  });

  it("reads the allowlist from GENUS_DASHBOARD_TRUSTED_PROXIES", async () => {
    vi.stubEnv("GENUS_DASHBOARD_TRUSTED_PROXIES", " 10.42.0.0/16 , 70.41.3.18 ");
    const { dashboardTrustedProxies, isTrustedProxy } = await import("@/lib/auth-local");
    expect(dashboardTrustedProxies()).toEqual(["10.42.0.0/16", "70.41.3.18"]);
    expect(isTrustedProxy("10.42.1.1")).toBe(true);
    expect(isTrustedProxy("203.0.113.7")).toBe(false);
  });

  it("trusts nobody when the variable is unset", async () => {
    const { dashboardTrustedProxies, isTrustedProxy } = await import("@/lib/auth-local");
    expect(dashboardTrustedProxies()).toEqual([]);
    expect(isTrustedProxy("127.0.0.1")).toBe(false);
  });
});

describe("the server-only session facade", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("exposes the refresh token to server callers but never to the browser", async () => {
    const { bridgeSessionCallback, publicBridgeSessionCallback } = await import("@/lib/auth");
    const token = {
      bridgeAccess: "bridge-access-token",
      bridgeRefresh: "bridge-refresh-token",
      role: "owner",
      tenantId: "default",
    } as JWT;

    const server = await bridgeSessionCallback({
      session: { user: { email: "alice@example.com" }, expires: "" } as never,
      token,
    });
    expect(server.bridgeRefresh).toBe("bridge-refresh-token");

    const browser = await publicBridgeSessionCallback({
      session: { user: { email: "alice@example.com" }, expires: "" } as never,
      token,
    });
    expect(browser.bridgeRefresh).toBeUndefined();
    expect(JSON.stringify(browser)).not.toContain("bridge-refresh-token");
  });
});
