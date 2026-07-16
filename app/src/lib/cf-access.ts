/**
 * Cloudflare Access header trust — optional, env-gated sign-in path.
 *
 * When the dashboard is deployed behind a Cloudflare Access policy, Cloudflare
 * injects a `Cf-Access-Jwt-Assertion` JWT (signed by the team's JWKS) on every
 * request that passed the edge check. With `CF_ACCESS_TEAM_DOMAIN` and
 * `CF_ACCESS_AUD` set, the dashboard verifies that JWT and establishes its
 * Auth.js session from it — the operator authenticates once, at the edge,
 * instead of facing a second IdP prompt. Verification is full JWT validation
 * (signature against the team JWKS, issuer, audience, expiry) — never header
 * trust by presence.
 */

import { createRemoteJWKSet, jwtVerify } from "jose";

export const CF_JWT_HEADER = "cf-access-jwt-assertion";

export type CfVerifiedClaims = {
  issuer: string;
  subject: string;
  email: string;
  display_name: string;
  email_verified: true;
};

export function cfTeamDomain(): string {
  return (process.env.CF_ACCESS_TEAM_DOMAIN || "").trim().replace(/\/+$/, "");
}

export function cfAudiences(): string[] {
  return (process.env.CF_ACCESS_AUD || "")
    .split(",")
    .map((aud) => aud.trim())
    .filter(Boolean);
}

export function cfAccessEnabled(): boolean {
  return Boolean(cfTeamDomain()) && cfAudiences().length > 0;
}

// The remote JWKS is cached (and refetched on unknown-kid) by jose; key it by
// team domain so env changes in tests get a fresh set.
let jwks: ReturnType<typeof createRemoteJWKSet> | null = null;
let jwksTeamDomain = "";

export function resetCfJwks(): void {
  jwks = null;
  jwksTeamDomain = "";
}

function jwksFor(teamDomain: string): ReturnType<typeof createRemoteJWKSet> {
  if (!jwks || jwksTeamDomain !== teamDomain) {
    jwks = createRemoteJWKSet(new URL(`${teamDomain}/cdn-cgi/access/certs`));
    jwksTeamDomain = teamDomain;
  }
  return jwks;
}

/**
 * Verify a Cloudflare Access JWT and normalize it into the claim shape the
 * bridge SSO exchange takes. Returns null on ANY failure — including service
 * tokens, which carry no user subject/email and must not become dashboard
 * sessions. Cloudflare only asserts emails it authenticated (OTP or upstream
 * IdP), so a verified token implies a verified email.
 */
export async function verifyCfAccessJwt(jwt: string): Promise<CfVerifiedClaims | null> {
  const teamDomain = cfTeamDomain();
  const audiences = cfAudiences();
  if (!teamDomain || audiences.length === 0 || !jwt) return null;
  try {
    const { payload } = await jwtVerify(jwt, jwksFor(teamDomain), {
      issuer: teamDomain,
      audience: audiences,
    });
    const subject = typeof payload.sub === "string" ? payload.sub.trim() : "";
    const email = typeof payload.email === "string" ? payload.email.trim() : "";
    if (!subject || !email) return null;
    const name = typeof payload.name === "string" ? payload.name.trim() : "";
    return {
      issuer: teamDomain,
      subject,
      email,
      display_name: name || email,
      email_verified: true,
    };
  } catch {
    return null;
  }
}

export type SignInMode = "cf-redirect" | "oidc-button" | "none";

/**
 * Decide what /signin should do. An error query param always suppresses the
 * Cloudflare auto-redirect: a failed CF sign-in lands back on /signin?error=…
 * and must render a page instead of looping through the handler again.
 */
export function resolveSignInMode(options: {
  hasCfHeader: boolean;
  cfEnabled: boolean;
  oidcConfigured: boolean;
  errorParam?: string;
}): SignInMode {
  if (options.cfEnabled && options.hasCfHeader && !options.errorParam) return "cf-redirect";
  if (options.oidcConfigured) return "oidc-button";
  return "none";
}
