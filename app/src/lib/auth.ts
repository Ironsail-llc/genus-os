/**
 * Auth.js (next-auth v5) — the dashboard's SSO client.
 *
 * Auth.js owns the OIDC handshake with the org IdP (generic OIDC provider,
 * env-driven: works with Okta / Entra / Auth0 / Keycloak / any OIDC IdP; SAML
 * IdPs are reached via their OIDC endpoint or a SAML bridge — see README).
 *
 * After the IdP verifies the user, we exchange the verified claims for
 * BRIDGE-issued JWTs (the bridge is the token authority + RBAC enforcement
 * point). Those tokens live inside the Auth.js session JWT and are forwarded as
 * `Authorization: Bearer` on every proxied backend call. The bridge stays
 * IdP-agnostic — it only verifies its own JWT.
 *
 * Runs in the Node runtime (the bridge exchange/refresh does fetch + reads
 * server-only env). The request proxy verifies the Auth.js session and requires
 * the bridge credential before allowing private traffic.
 */

import NextAuth from "next-auth";
import type { NextAuthConfig, Profile, Session } from "next-auth";
import type { JWT } from "next-auth/jwt";

import { getServiceUrl } from "@/lib/services/registry";

const OIDC_ISSUER = process.env.AUTH_OIDC_ISSUER;
const BRIDGE_URL = () => getServiceUrl("bridge") || "http://localhost:9100";
const SSO_SECRET = () => process.env.GENUS_BRIDGE_SSO_SECRET || "";

// Refresh the bridge access token a minute before its 15-min TTL.
const ACCESS_SKEW_MS = 14 * 60 * 1000;

type SsoResult = {
  access_token: string;
  refresh_token: string;
  user: { id: string; email: string; display_name: string; role: string; tenant_id: string };
};

type BridgeAuthError =
  | "BridgeRefreshFailed"
  | "BridgeSessionInvalid";

type JwtCallbackParams = {
  token: JWT;
  account?: { provider?: string } | null;
  profile?: Profile;
  trigger?: "signIn" | "signUp" | "update";
};

type SignInCallbackParams = {
  account?: { provider?: string } | null;
  profile?: Profile;
};

function profileString(profile: Profile | undefined, key: string): string {
  const value = profile?.[key];
  return typeof value === "string" ? value.trim() : "";
}

function verifiedOidcClaims(profile: Profile | undefined): {
  issuer: string;
  subject: string;
  email: string;
  display_name: string;
  email_verified: true;
} | null {
  if (profile?.email_verified !== true) return null;

  const issuer = profileString(profile, "iss") || OIDC_ISSUER?.trim() || "";
  const subject = profileString(profile, "sub");
  const email = profileString(profile, "email");
  if (!issuer || !subject || !email) return null;

  return {
    issuer,
    subject,
    email,
    display_name: profileString(profile, "name") || email,
    email_verified: true,
  };
}

function isSsoResult(value: unknown): value is SsoResult {
  if (!value || typeof value !== "object") return false;
  const result = value as Partial<SsoResult>;
  const user = result.user as Partial<SsoResult["user"]> | undefined;
  return (
    typeof result.access_token === "string" &&
    result.access_token.length > 0 &&
    typeof result.refresh_token === "string" &&
    result.refresh_token.length > 0 &&
    !!user &&
    typeof user.id === "string" &&
    typeof user.email === "string" &&
    typeof user.display_name === "string" &&
    typeof user.role === "string" &&
    typeof user.tenant_id === "string"
  );
}

async function bridgeSsoExchange(claims: {
  issuer: string;
  subject: string;
  email: string;
  display_name: string;
  email_verified: true;
}): Promise<SsoResult | null> {
  if (!SSO_SECRET()) return null;
  try {
    const res = await fetch(`${BRIDGE_URL()}/api/auth/sso`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Auth": SSO_SECRET() },
      body: JSON.stringify(claims),
    });
    if (!res.ok) return null;
    const result: unknown = await res.json();
    return isSsoResult(result) ? result : null;
  } catch {
    return null;
  }
}

async function bridgeRefresh(refreshToken: string): Promise<SsoResult | null> {
  try {
    const res = await fetch(`${BRIDGE_URL()}/api/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) return null;
    const result: unknown = await res.json();
    return isSsoResult(result) ? result : null;
  } catch {
    return null;
  }
}

function applyBridgeTokens(token: JWT, result: SsoResult): JWT {
  token.bridgeAccess = result.access_token;
  token.bridgeRefresh = result.refresh_token;
  token.role = result.user.role;
  token.tenantId = result.user.tenant_id;
  token.accessExpiresAt = Date.now() + ACCESS_SKEW_MS;
  delete token.bridgeAuthError;
  return token;
}

function invalidateBridgeToken(token: JWT, error: BridgeAuthError): JWT {
  delete token.bridgeAccess;
  delete token.bridgeRefresh;
  delete token.role;
  delete token.tenantId;
  delete token.accessExpiresAt;
  token.bridgeAuthError = error;
  return token;
}

export function oidcSignInAllowed({
  account,
  profile,
}: SignInCallbackParams): boolean {
  return account?.provider === "oidc" && verifiedOidcClaims(profile) !== null;
}

export async function bridgeJwtCallback({
  token,
  profile,
  account,
  trigger,
}: JwtCallbackParams): Promise<JWT> {
  // First sign-in: exchange only a verified, complete OIDC identity. Throwing
  // aborts Auth.js sign-in instead of leaving a dashboard session half-created.
  if (account || trigger === "signIn" || trigger === "signUp") {
    const claims = verifiedOidcClaims(profile);
    if (account?.provider !== "oidc" || !claims) {
      throw new Error("verified OIDC identity required");
    }
    const exchange = await bridgeSsoExchange(claims);
    if (!exchange) {
      throw new Error("bridge sign-in exchange failed");
    }
    return applyBridgeTokens(token, exchange);
  }

  // Existing Auth.js cookies are not usable unless they still contain the
  // complete bridge credential set created above.
  if (
    typeof token.bridgeAccess !== "string" ||
    typeof token.bridgeRefresh !== "string" ||
    typeof token.accessExpiresAt !== "number" ||
    token.bridgeAuthError
  ) {
    return invalidateBridgeToken(token, "BridgeSessionInvalid");
  }

  if (Date.now() <= token.accessExpiresAt) return token;

  const refreshed = await bridgeRefresh(token.bridgeRefresh);
  if (!refreshed) {
    return invalidateBridgeToken(token, "BridgeRefreshFailed");
  }
  return applyBridgeTokens(token, refreshed);
}

export async function bridgeSessionCallback({
  session,
  token,
}: {
  session: Session;
  token: JWT;
}): Promise<Session> {
  const bridgeAccess =
    typeof token.bridgeAccess === "string" && !token.bridgeAuthError
      ? token.bridgeAccess
      : undefined;

  if (!bridgeAccess) {
    // The request proxy requires both user and bridgeAccess. Removing both the
    // user and credential makes a failed refresh immediately unusable even if
    // the encrypted Auth.js cookie has not expired yet.
    delete session.user;
    delete session.bridgeAccess;
    delete session.role;
    delete session.tenantId;
    session.authError = (token.bridgeAuthError as BridgeAuthError | undefined) ??
      "BridgeSessionInvalid";
    return session;
  }

  session.bridgeAccess = bridgeAccess;
  session.role = token.role as string | undefined;
  session.tenantId = token.tenantId as string | undefined;
  delete session.authError;
  if (session.user) session.user.role = token.role as string | undefined;
  return session;
}

/**
 * Shape the browser-visible Auth.js session without serializing backend
 * bearer or refresh credentials. The non-secret marker lets client UI know
 * whether the BFF is usable; authorization still happens server-side.
 */
export async function publicBridgeSessionCallback({
  session,
  token,
}: {
  session: Session;
  token: JWT;
}): Promise<Session> {
  const internal = await bridgeSessionCallback({ session, token });
  internal.backendAuthorized = Boolean(
    internal.user && internal.bridgeAccess && !internal.authError,
  );
  delete internal.bridgeAccess;
  return internal;
}

export const authConfig = {
  trustHost: true,
  pages: { signIn: "/signin" },
  session: { strategy: "jwt" },
  providers: [
    {
      id: "oidc",
      name: process.env.AUTH_OIDC_NAME || "SSO",
      type: "oidc",
      issuer: OIDC_ISSUER,
      clientId: process.env.AUTH_OIDC_CLIENT_ID,
      clientSecret: process.env.AUTH_OIDC_CLIENT_SECRET,
    },
  ],
  callbacks: {
    signIn: oidcSignInAllowed,
    jwt: bridgeJwtCallback,
    session: publicBridgeSessionCallback,
  },
} satisfies NextAuthConfig;

const publicAuth = NextAuth(authConfig);
export const { handlers, signIn, signOut } = publicAuth;

// Server-side BFF/proxy calls need the Bridge credential after Auth.js has
// decrypted the JWT and performed refresh rotation. Keep that richer session
// callback on a separate server-only Auth.js facade; browser handlers above
// always use `publicBridgeSessionCallback` and therefore never serialize it.
const serverAuthConfig = {
  ...authConfig,
  callbacks: {
    ...authConfig.callbacks,
    session: bridgeSessionCallback,
  },
} satisfies NextAuthConfig;

export const { auth } = NextAuth(serverAuthConfig);
