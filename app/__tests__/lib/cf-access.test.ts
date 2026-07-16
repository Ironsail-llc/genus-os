// @vitest-environment node
// jose does WebCrypto signing/verification; jsdom's cross-realm Uint8Array
// breaks it, and nothing here needs a DOM.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SignJWT, exportJWK, generateKeyPair } from "jose";

import {
  CF_JWT_HEADER,
  cfAccessEnabled,
  resetCfJwks,
  resolveSignInMode,
  verifyCfAccessJwt,
} from "@/lib/cf-access";

const TEAM = "https://team.example.com";
const AUD = "aud-tag-1";

let privateKey: CryptoKey;
let jwksResponse: () => Response;

async function makeKeys() {
  const pair = await generateKeyPair("RS256");
  privateKey = pair.privateKey;
  const jwk = await exportJWK(pair.publicKey);
  const body = JSON.stringify({ keys: [{ ...jwk, kid: "test-key", alg: "RS256", use: "sig" }] });
  jwksResponse = () =>
    new Response(body, { status: 200, headers: { "content-type": "application/json" } });
}

async function signToken(overrides: {
  iss?: string;
  aud?: string;
  sub?: string;
  email?: string;
  name?: string;
  expired?: boolean;
}) {
  const jwt = new SignJWT({
    email: overrides.email,
    name: overrides.name,
  })
    .setProtectedHeader({ alg: "RS256", kid: "test-key" })
    .setIssuer(overrides.iss ?? TEAM)
    .setAudience(overrides.aud ?? AUD)
    .setSubject(overrides.sub ?? "cf-user-uuid-1")
    .setIssuedAt();
  if (overrides.expired) {
    jwt.setExpirationTime(Math.floor(Date.now() / 1000) - 3600);
  } else {
    jwt.setExpirationTime("5m");
  }
  return jwt.sign(privateKey);
}

describe("Cloudflare Access JWT verification", () => {
  beforeEach(async () => {
    vi.stubEnv("CF_ACCESS_TEAM_DOMAIN", TEAM);
    vi.stubEnv("CF_ACCESS_AUD", AUD);
    resetCfJwks();
    await makeKeys();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jwksResponse()),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    resetCfJwks();
  });

  it("exposes the canonical header name", () => {
    expect(CF_JWT_HEADER).toBe("cf-access-jwt-assertion");
  });

  it("verifies a token signed by the team JWKS", async () => {
    const token = await signToken({ email: "operator@example.com", name: "Op" });
    const claims = await verifyCfAccessJwt(token);
    expect(claims).toEqual({
      issuer: TEAM,
      subject: "cf-user-uuid-1",
      email: "operator@example.com",
      display_name: "Op",
      email_verified: true,
    });
  });

  it("falls back to the email as display name", async () => {
    const token = await signToken({ email: "operator@example.com" });
    const claims = await verifyCfAccessJwt(token);
    expect(claims?.display_name).toBe("operator@example.com");
  });

  it("accepts any audience in the comma-separated allowlist", async () => {
    vi.stubEnv("CF_ACCESS_AUD", `other-aud, ${AUD}`);
    resetCfJwks();
    const token = await signToken({ email: "operator@example.com" });
    const claims = await verifyCfAccessJwt(token);
    expect(claims?.email).toBe("operator@example.com");
  });

  it.each([
    ["wrong audience", { aud: "some-other-app", email: "a@example.com" }],
    ["wrong issuer", { iss: "https://evil.example.com", email: "a@example.com" }],
    ["expired token", { expired: true, email: "a@example.com" }],
  ])("rejects a token with %s", async (_label, overrides) => {
    const token = await signToken(overrides);
    expect(await verifyCfAccessJwt(token)).toBeNull();
  });

  it("rejects malformed tokens without throwing", async () => {
    expect(await verifyCfAccessJwt("not-a-jwt")).toBeNull();
    expect(await verifyCfAccessJwt("")).toBeNull();
  });

  it("rejects service tokens (no subject / no email)", async () => {
    expect(await verifyCfAccessJwt(await signToken({ sub: "", email: "a@example.com" }))).toBeNull();
    expect(await verifyCfAccessJwt(await signToken({ sub: "cf-user-uuid-1" }))).toBeNull();
  });

  it("refuses to verify when the env gate is off", async () => {
    const token = await signToken({ email: "operator@example.com" });
    vi.stubEnv("CF_ACCESS_TEAM_DOMAIN", "");
    expect(await verifyCfAccessJwt(token)).toBeNull();
  });

  it("rejects tokens signed with a non-RS256 algorithm", async () => {
    const es = await generateKeyPair("ES256");
    const esJwk = await exportJWK(es.publicKey);
    const body = JSON.stringify({
      keys: [
        { ...(await exportJWK((await generateKeyPair("RS256")).publicKey)), kid: "test-key", alg: "RS256", use: "sig" },
        { ...esJwk, kid: "es-key", alg: "ES256", use: "sig" },
      ],
    });
    jwksResponse = () =>
      new Response(body, { status: 200, headers: { "content-type": "application/json" } });
    resetCfJwks();
    const token = await new SignJWT({ email: "operator@example.com" })
      .setProtectedHeader({ alg: "ES256", kid: "es-key" })
      .setIssuer(TEAM)
      .setAudience(AUD)
      .setSubject("cf-user-uuid-1")
      .setIssuedAt()
      .setExpirationTime("5m")
      .sign(es.privateKey);
    expect(await verifyCfAccessJwt(token)).toBeNull();
  });

  it("logs the failure reason instead of swallowing it silently", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(await verifyCfAccessJwt("not-a-jwt")).toBeNull();
    expect(errorSpy).toHaveBeenCalled();
    expect(String(errorSpy.mock.calls[0][0])).toContain("cf-access");
    errorSpy.mockRestore();
  });

  it("logs a config error for a scheme-less team domain instead of failing silently", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.stubEnv("CF_ACCESS_TEAM_DOMAIN", "team.example.com");
    resetCfJwks();
    const token = await signToken({ email: "operator@example.com", iss: "team.example.com" });
    expect(await verifyCfAccessJwt(token)).toBeNull();
    expect(errorSpy).toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});

describe("cfAccessEnabled", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("requires both team domain and audience", () => {
    vi.stubEnv("CF_ACCESS_TEAM_DOMAIN", "");
    vi.stubEnv("CF_ACCESS_AUD", "");
    expect(cfAccessEnabled()).toBe(false);
    vi.stubEnv("CF_ACCESS_TEAM_DOMAIN", TEAM);
    expect(cfAccessEnabled()).toBe(false);
    vi.stubEnv("CF_ACCESS_AUD", AUD);
    expect(cfAccessEnabled()).toBe(true);
  });
});

describe("resolveSignInMode", () => {
  it("auto-redirects to the Cloudflare handler when enabled and header present", () => {
    expect(
      resolveSignInMode({ hasCfHeader: true, cfEnabled: true, oidcConfigured: false }),
    ).toBe("cf-redirect");
  });

  it("never auto-redirects when an error param is present (loop guard)", () => {
    expect(
      resolveSignInMode({
        hasCfHeader: true,
        cfEnabled: true,
        oidcConfigured: true,
        errorParam: "CloudflareAccessFailed",
      }),
    ).toBe("oidc-button");
    expect(
      resolveSignInMode({
        hasCfHeader: true,
        cfEnabled: true,
        oidcConfigured: false,
        errorParam: "CloudflareAccessFailed",
      }),
    ).toBe("none");
  });

  it("shows the OIDC button when CF is disabled or the header is missing", () => {
    expect(
      resolveSignInMode({ hasCfHeader: false, cfEnabled: true, oidcConfigured: true }),
    ).toBe("oidc-button");
    expect(
      resolveSignInMode({ hasCfHeader: true, cfEnabled: false, oidcConfigured: true }),
    ).toBe("oidc-button");
  });

  it("reports none when no sign-in method is configured", () => {
    expect(
      resolveSignInMode({ hasCfHeader: false, cfEnabled: false, oidcConfigured: false }),
    ).toBe("none");
  });
});
