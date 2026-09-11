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
          authorize?: (credentials: Record<string, unknown>) => Promise<unknown>;
        };
        id?: string;
        name?: string;
        authorize?: (credentials: Record<string, unknown>) => Promise<unknown>;
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

  it("is absent unless GENUS_LOCAL_LOGIN is exactly 'true'", async () => {
    for (const value of ["", "false", "1", "yes", "TRUE"]) {
      vi.resetModules();
      vi.stubEnv("GENUS_LOCAL_LOGIN", value);
      expect(await localProvider()).toBeUndefined();
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
