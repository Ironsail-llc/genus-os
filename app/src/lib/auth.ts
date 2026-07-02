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
 * server-only env). The Edge middleware does only a coarse cookie-presence gate.
 */

import NextAuth from "next-auth";

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

async function bridgeSsoExchange(claims: {
  issuer: string;
  subject: string;
  email: string;
  display_name: string;
}): Promise<SsoResult | null> {
  if (!SSO_SECRET()) return null;
  try {
    const res = await fetch(`${BRIDGE_URL()}/api/auth/sso`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Auth": SSO_SECRET() },
      body: JSON.stringify(claims),
    });
    return res.ok ? ((await res.json()) as SsoResult) : null;
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
    return res.ok ? ((await res.json()) as SsoResult) : null;
  } catch {
    return null;
  }
}

export const { handlers, auth, signIn, signOut } = NextAuth({
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
    async jwt({ token, profile, account }) {
      // First sign-in: exchange the verified IdP identity for bridge tokens.
      if (account && profile) {
        const ex = await bridgeSsoExchange({
          issuer: (profile.iss as string) || OIDC_ISSUER || "",
          subject: (profile.sub as string) || "",
          email: (profile.email as string) || "",
          display_name: (profile.name as string) || (profile.email as string) || "",
        });
        if (ex) {
          token.bridgeAccess = ex.access_token;
          token.bridgeRefresh = ex.refresh_token;
          token.role = ex.user.role;
          token.tenantId = ex.user.tenant_id;
          token.accessExpiresAt = Date.now() + ACCESS_SKEW_MS;
        }
        return token;
      }
      // Subsequent calls: refresh the bridge access token when near expiry.
      if (token.bridgeRefresh && Date.now() > ((token.accessExpiresAt as number) || 0)) {
        const r = await bridgeRefresh(token.bridgeRefresh as string);
        if (r) {
          token.bridgeAccess = r.access_token;
          token.bridgeRefresh = r.refresh_token;
          token.role = r.user.role;
          token.accessExpiresAt = Date.now() + ACCESS_SKEW_MS;
        } else {
          token.bridgeAccess = undefined; // refresh failed → force re-login
        }
      }
      return token;
    },
    async session({ session, token }) {
      // Expose the bridge access token + role to server-side callers (proxies).
      session.bridgeAccess = token.bridgeAccess as string | undefined;
      session.role = token.role as string | undefined;
      session.tenantId = token.tenantId as string | undefined;
      if (session.user) session.user.role = token.role as string | undefined;
      return session;
    },
  },
});
