import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Profile, Session } from "next-auth";
import type { JWT } from "next-auth/jwt";

import {
  bridgeJwtCallback,
  bridgeSessionCallback,
  publicBridgeSessionCallback,
  signInAllowed,
} from "@/lib/auth";

const account = { provider: "oidc" };
const cfAccount = { provider: "cloudflare-access" };
const cfClaims = {
  issuer: "https://team.example.com",
  subject: "cf-user-uuid-1",
  email: "operator@example.com",
  display_name: "Test Operator",
  email_verified: true as const,
};
const cfUser = {
  id: "https://team.example.com|cf-user-uuid-1",
  email: "operator@example.com",
  name: "Test Operator",
  cfClaims,
};
const verifiedProfile = {
  iss: "https://idp.example.test",
  sub: "subject-1",
  email: "operator@example.com",
  name: "Test Operator",
  email_verified: true,
} satisfies Profile;

const exchangeResult = {
  access_token: "bridge-access-token",
  refresh_token: "bridge-refresh-token",
  user: {
    id: "user-1",
    email: "operator@example.com",
    display_name: "Test Operator",
    role: "member",
    tenant_id: "default",
  },
};

function response(ok: boolean, body: unknown = {}): Response {
  return {
    ok,
    json: async () => body,
  } as Response;
}

describe("dashboard OIDC and Bridge session binding", () => {
  beforeEach(() => {
    vi.stubEnv("GENUS_BRIDGE_SSO_SECRET", "dashboard-shared-secret");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("rejects an OIDC identity without an explicitly verified email", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const profile = {
      ...verifiedProfile,
      email_verified: false,
    } satisfies Profile;

    expect(signInAllowed({ account, profile })).toBe(false);
    await expect(
      bridgeJwtCallback({ token: {}, account, profile, trigger: "signIn" }),
    ).rejects.toThrow("verified identity required");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["bridge denied", "bridge unavailable"])(
    "fails the Auth.js sign-in when the %s exchange",
    async (failure) => {
      const fetchMock = vi.fn();
      if (failure === "bridge denied") {
        fetchMock.mockResolvedValue(response(false));
      } else {
        fetchMock.mockRejectedValue(new Error("connection refused"));
      }
      vi.stubGlobal("fetch", fetchMock);

      expect(signInAllowed({ account, profile: verifiedProfile })).toBe(true);
      await expect(
        bridgeJwtCallback({
          token: {},
          account,
          profile: verifiedProfile,
          trigger: "signIn",
        }),
      ).rejects.toThrow("bridge sign-in exchange failed");
    },
  );

  it("binds a usable session only after a successful verified exchange", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(true, exchangeResult));
    vi.stubGlobal("fetch", fetchMock);

    const token = await bridgeJwtCallback({
      token: {},
      account,
      profile: verifiedProfile,
      trigger: "signIn",
    });

    expect(token.bridgeAccess).toBe("bridge-access-token");
    expect(token.bridgeRefresh).toBe("bridge-refresh-token");
    const request = fetchMock.mock.calls[0];
    const body = JSON.parse((request[1] as RequestInit).body as string) as {
      email_verified: boolean;
    };
    expect(body.email_verified).toBe(true);
  });

  it("clears every authorization field and user identity after refresh failure", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(false));
    vi.stubGlobal("fetch", fetchMock);
    const token: JWT = {
      bridgeAccess: "expired-access-token",
      bridgeRefresh: "one-use-refresh-token",
      accessExpiresAt: 0,
      role: "owner",
      tenantId: "default",
    };

    const invalidated = await bridgeJwtCallback({ token });
    expect(invalidated).toMatchObject({
      bridgeAuthError: "BridgeRefreshFailed",
    });
    expect(invalidated.bridgeAccess).toBeUndefined();
    expect(invalidated.bridgeRefresh).toBeUndefined();
    expect(invalidated.role).toBeUndefined();
    expect(invalidated.tenantId).toBeUndefined();

    const session: Session = {
      expires: "2099-01-01T00:00:00.000Z",
      user: { name: "Test Operator", email: "operator@example.com" },
    };
    const exposed = await bridgeSessionCallback({
      session,
      token: invalidated,
    });
    expect(exposed.user).toBeUndefined();
    expect(exposed.bridgeAccess).toBeUndefined();
    expect(exposed.role).toBeUndefined();
    expect(exposed.tenantId).toBeUndefined();
    expect(exposed.authError).toBe("BridgeRefreshFailed");
  });

  it("accepts a cloudflare-access sign-in carrying verified claims", () => {
    expect(signInAllowed({ account: cfAccount, user: cfUser })).toBe(true);
  });

  it("rejects a cloudflare-access sign-in without well-formed claims", () => {
    expect(
      signInAllowed({
        account: cfAccount,
        user: { ...cfUser, cfClaims: undefined },
      }),
    ).toBe(false);
    expect(
      signInAllowed({
        account: cfAccount,
        user: { ...cfUser, cfClaims: { ...cfClaims, email: "" } },
      }),
    ).toBe(false);
    const unverifiedClaims = {
      ...cfClaims,
      email_verified: false,
    } as unknown as typeof cfClaims;
    expect(
      signInAllowed({
        account: cfAccount,
        user: { ...cfUser, cfClaims: unverifiedClaims },
      }),
    ).toBe(false);
  });

  it("rejects sign-ins from unknown providers", () => {
    expect(
      signInAllowed({ account: { provider: "credentials" }, user: cfUser }),
    ).toBe(false);
    expect(signInAllowed({ account: null, profile: verifiedProfile })).toBe(
      false,
    );
  });

  it("exchanges cloudflare-access claims with the bridge on sign-in", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(true, exchangeResult));
    vi.stubGlobal("fetch", fetchMock);

    const token = await bridgeJwtCallback({
      token: {},
      account: cfAccount,
      user: cfUser,
      trigger: "signIn",
    });

    expect(token.bridgeAccess).toBe("bridge-access-token");
    const body = JSON.parse(
      (fetchMock.mock.calls[0][1] as RequestInit).body as string,
    ) as Record<string, unknown>;
    expect(body).toMatchObject({
      issuer: "https://team.example.com",
      subject: "cf-user-uuid-1",
      email: "operator@example.com",
      email_verified: true,
    });
  });

  it("fails a cloudflare-access sign-in without claims instead of half-creating a session", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      bridgeJwtCallback({
        token: {},
        account: cfAccount,
        user: { ...cfUser, cfClaims: undefined },
        trigger: "signIn",
      }),
    ).rejects.toThrow("verified identity required");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never serializes the Bridge bearer into the browser-visible session", async () => {
    const session: Session = {
      expires: "2099-01-01T00:00:00.000Z",
      user: { name: "Test Operator", email: "operator@example.com" },
    };
    const token: JWT = {
      bridgeAccess: "secret-bridge-bearer",
      bridgeRefresh: "secret-refresh-token",
      accessExpiresAt: Date.now() + 60_000,
      role: "member",
      tenantId: "default",
    };

    const exposed = await publicBridgeSessionCallback({ session, token });

    expect(exposed.backendAuthorized).toBe(true);
    expect(exposed.bridgeAccess).toBeUndefined();
    expect(JSON.stringify(exposed)).not.toContain("secret-bridge-bearer");
    expect(JSON.stringify(exposed)).not.toContain("secret-refresh-token");
  });
});

describe("conditional provider registration", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  async function providerIds(env: Record<string, string>): Promise<string[]> {
    vi.resetModules();
    for (const [key, value] of Object.entries({
      AUTH_OIDC_ISSUER: "",
      AUTH_OIDC_CLIENT_ID: "",
      CF_ACCESS_TEAM_DOMAIN: "",
      CF_ACCESS_AUD: "",
      ...env,
    })) {
      vi.stubEnv(key, value);
    }
    const { authConfig } = await import("@/lib/auth");
    return authConfig.providers.map((provider) => {
      const p = provider as { id?: string; options?: { id?: string } };
      return p.options?.id ?? p.id ?? "";
    });
  }

  it("omits the oidc provider when the client id is missing (no InvalidEndpoints noise)", async () => {
    const ids = await providerIds({
      AUTH_OIDC_ISSUER: "https://accounts.example.com",
    });
    expect(ids).not.toContain("oidc");
  });

  it("registers cloudflare-access only when its env gate is set", async () => {
    const ids = await providerIds({
      CF_ACCESS_TEAM_DOMAIN: "https://team.example.com",
      CF_ACCESS_AUD: "aud-tag-1",
    });
    expect(ids).toContain("cloudflare-access");
    expect(ids).not.toContain("oidc");
  });

  it("registers the local provider even with GENUS_LOCAL_LOGIN unset", async () => {
    // The first-run wizard turns local login on for the INSTANCE, in
    // config.yaml, which this process does not read. A boot-time env check
    // therefore said "off" for the whole of first run: the provider was never
    // registered, `signIn("local", …)` threw CredentialsSignin, and the
    // wizard's documented happy path ended at "restart the dashboard".
    //
    // Registering it is not enabling it — authorize() asks the bridge, and the
    // bridge 404s its login route whenever local login is off.
    const ids = await providerIds({ GENUS_LOCAL_LOGIN: "" });
    expect(ids).toContain("local");
  });

  it("registers both providers when both are fully configured", async () => {
    const ids = await providerIds({
      AUTH_OIDC_ISSUER: "https://accounts.example.com",
      AUTH_OIDC_CLIENT_ID: "client-1",
      CF_ACCESS_TEAM_DOMAIN: "https://team.example.com",
      CF_ACCESS_AUD: "aud-tag-1",
    });
    expect(ids).toContain("oidc");
    expect(ids).toContain("cloudflare-access");
  });
});

describe("the local provider asks the bridge, not the environment", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  type Authorize = (
    credentials: Record<string, string>,
    request: Request | undefined,
  ) => Promise<unknown>;

  async function localAuthorize(): Promise<Authorize> {
    vi.resetModules();
    vi.stubEnv("GENUS_LOCAL_LOGIN", "");
    const { authConfig } = await import("@/lib/auth");
    const provider = authConfig.providers.find((candidate) => {
      const p = candidate as { id?: string; options?: { id?: string } };
      return (p.options?.id ?? p.id) === "local";
    }) as
      | { options?: { authorize?: Authorize }; authorize?: Authorize }
      | undefined;
    const authorize = provider?.options?.authorize ?? provider?.authorize;
    if (!authorize) throw new Error("the local provider was not registered");
    return authorize;
  }

  function bridge(methods: { local: boolean }, login?: unknown) {
    return vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/auth/methods")) {
        return { ok: true, status: 200, json: async () => methods } as Response;
      }
      if (login === undefined) throw new Error(`unexpected call to ${url}`);
      return { ok: true, status: 200, json: async () => login } as Response;
    });
  }

  it("refuses when the bridge says local login is off", async () => {
    const fetchMock = bridge({ local: false });
    vi.stubGlobal("fetch", fetchMock);
    const authorize = await localAuthorize();

    const result = await authorize(
      { email: "alice@example.test", password: "correct-horse-battery-staple" },
      undefined,
    );

    expect(result).toBeNull();
    // And it never spent a credential attempt on the bridge's login route.
    expect(fetchMock.mock.calls.map((call) => String(call[0]))).toEqual([
      expect.stringContaining("/api/auth/methods"),
    ]);
  });

  it("signs in when the bridge says local login is on, with the env unset", async () => {
    vi.stubGlobal(
      "fetch",
      bridge(
        { local: true },
        {
          access_token: "access-token-value",
          refresh_token: "refresh-token-value",
          user: {
            id: "user-1",
            email: "alice@example.test",
            display_name: "Alice",
            role: "owner",
            tenant_id: "default",
          },
        },
      ),
    );
    const authorize = await localAuthorize();

    const result = (await authorize(
      { email: "alice@example.test", password: "correct-horse-battery-staple" },
      undefined,
    )) as { email?: string } | null;

    expect(result?.email).toBe("alice@example.test");
  });

  it("refuses when the bridge cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connection refused");
      }),
    );
    const authorize = await localAuthorize();

    await expect(
      authorize(
        {
          email: "alice@example.test",
          password: "correct-horse-battery-staple",
        },
        undefined,
      ),
    ).resolves.toBeNull();
  });
});

describe("the mfa-setup hint across a token refresh", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  const refreshed = {
    access_token: "new-access-token",
    refresh_token: "new-refresh-token",
    user: {
      id: "user-1",
      email: "operator@example.com",
      display_name: "Test Operator",
      role: "owner",
      tenant_id: "default",
    },
  };

  // The banner re-reads /api/auth/me, which is authoritative, so a stale hint is
  // cosmetic — but `applyBridgeTokens` never touched `mfaSetupRequired`, so the
  // value set at local sign-in survived every refresh for the life of the
  // Auth.js cookie and could end up contradicting the panel.
  it("is cleared when the refresh response does not carry it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response(true, refreshed)),
    );
    const token = await bridgeJwtCallback({
      token: {
        bridgeAccess: "expired-access-token",
        bridgeRefresh: "one-use-refresh-token",
        accessExpiresAt: 0,
        role: "owner",
        tenantId: "default",
        mfaSetupRequired: true,
      } as JWT,
    });
    expect(token.bridgeAccess).toBe("new-access-token");
    expect(token.mfaSetupRequired).toBeUndefined();
  });

  it("is refreshed from the response when the bridge does report it", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          response(true, { ...refreshed, mfa_setup_required: true }),
        ),
    );
    const token = await bridgeJwtCallback({
      token: {
        bridgeAccess: "expired-access-token",
        bridgeRefresh: "one-use-refresh-token",
        accessExpiresAt: 0,
        role: "owner",
        tenantId: "default",
      } as JWT,
    });
    expect(token.mfaSetupRequired).toBe(true);
  });
});
