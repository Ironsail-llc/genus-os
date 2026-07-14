import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Profile, Session } from "next-auth";
import type { JWT } from "next-auth/jwt";

import {
  bridgeJwtCallback,
  bridgeSessionCallback,
  oidcSignInAllowed,
  publicBridgeSessionCallback,
} from "@/lib/auth";

const account = { provider: "oidc" };
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
    const profile = { ...verifiedProfile, email_verified: false } satisfies Profile;

    expect(oidcSignInAllowed({ account, profile })).toBe(false);
    await expect(
      bridgeJwtCallback({ token: {}, account, profile, trigger: "signIn" }),
    ).rejects.toThrow("verified OIDC identity required");
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

      expect(oidcSignInAllowed({ account, profile: verifiedProfile })).toBe(true);
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
    expect(invalidated).toMatchObject({ bridgeAuthError: "BridgeRefreshFailed" });
    expect(invalidated.bridgeAccess).toBeUndefined();
    expect(invalidated.bridgeRefresh).toBeUndefined();
    expect(invalidated.role).toBeUndefined();
    expect(invalidated.tenantId).toBeUndefined();

    const session: Session = {
      expires: "2099-01-01T00:00:00.000Z",
      user: { name: "Test Operator", email: "operator@example.com" },
    };
    const exposed = await bridgeSessionCallback({ session, token: invalidated });
    expect(exposed.user).toBeUndefined();
    expect(exposed.bridgeAccess).toBeUndefined();
    expect(exposed.role).toBeUndefined();
    expect(exposed.tenantId).toBeUndefined();
    expect(exposed.authError).toBe("BridgeRefreshFailed");
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
