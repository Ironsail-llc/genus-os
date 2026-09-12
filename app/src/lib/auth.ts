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

import NextAuth, { CredentialsSignin } from "next-auth";
import type { NextAuthConfig, Profile, Session, User } from "next-auth";
import type { JWT } from "next-auth/jwt";
import Credentials from "next-auth/providers/credentials";

import type { LocalLoginResult } from "@/lib/auth-local";
import {
  bridgeLocalLogin,
  bridgeLocalLoginOffered,
  clientIpFromRequest,
} from "@/lib/auth-local";
import type { CfVerifiedClaims } from "@/lib/cf-access";
import {
  CF_JWT_HEADER,
  cfAccessEnabled,
  verifyCfAccessJwt,
} from "@/lib/cf-access";
import { getServiceUrl } from "@/lib/services/registry";

const OIDC_ISSUER = process.env.AUTH_OIDC_ISSUER;
const BRIDGE_URL = () => getServiceUrl("bridge") || "http://localhost:9100";
const SSO_SECRET = () => process.env.GENUS_BRIDGE_SSO_SECRET || "";

// Refresh the bridge access token a minute before its 15-min TTL.
const ACCESS_SKEW_MS = 14 * 60 * 1000;

type SsoResult = {
  access_token: string;
  refresh_token: string;
  user: {
    id: string;
    email: string;
    display_name: string;
    role: string;
    tenant_id: string;
  };
};

type BridgeAuthError = "BridgeRefreshFailed" | "BridgeSessionInvalid";

type JwtCallbackParams = {
  token: JWT;
  account?: { provider?: string } | null;
  profile?: Profile;
  user?: User;
  trigger?: "signIn" | "signUp" | "update";
};

type SignInCallbackParams = {
  account?: { provider?: string } | null;
  profile?: Profile;
  user?: User;
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

// Credentials-style providers carry no `profile`; the authorize() return value
// arrives as `user`. Re-validate the claim shape here so the jwt callback never
// trusts a malformed object.
function cfClaimsFromUser(user: User | undefined): CfVerifiedClaims | null {
  const claims = user?.cfClaims;
  if (!claims || claims.email_verified !== true) return null;
  if (
    !claims.issuer?.trim() ||
    !claims.subject?.trim() ||
    !claims.email?.trim()
  )
    return null;
  return claims;
}

/**
 * Thrown by the local provider's `authorize()` when the password was right and
 * a one-time code is owed. Auth.js surfaces `code` as the `error` query
 * parameter, which is how /signin knows to render the code field — and it is
 * the ONLY distinguishable sign-in failure, exactly as the bridge decided.
 */
export class MfaRequiredError extends CredentialsSignin {
  code = "mfa_required";
}

// Credentials-style providers carry no `profile`; the authorize() return value
// arrives as `user`. Re-validate here so the jwt callback never trusts a
// malformed object — same reasoning as cfClaimsFromUser above.
function localTokensFromUser(user: User | undefined): LocalLoginResult | null {
  const tokens = user?.localTokens;
  if (!tokens) return null;
  if (!tokens.access_token?.trim() || !tokens.refresh_token?.trim())
    return null;
  if (!tokens.user?.id?.trim() || !tokens.user?.tenant_id?.trim()) return null;
  return tokens;
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
      headers: {
        "Content-Type": "application/json",
        "X-Bridge-Auth": SSO_SECRET(),
      },
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
  // Refreshed, or dropped. Left untouched, the hint set at local sign-in
  // survived every refresh for the life of the Auth.js cookie and could end up
  // contradicting `/api/auth/me`, which is the authoritative answer the banner
  // and the security panel actually render from.
  const reported = (result as { mfa_setup_required?: unknown })
    .mfa_setup_required;
  if (typeof reported === "boolean") token.mfaSetupRequired = reported;
  else delete token.mfaSetupRequired;
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

function applyLocalTokens(token: JWT, result: LocalLoginResult): JWT {
  token.bridgeAccess = result.access_token;
  token.bridgeRefresh = result.refresh_token;
  token.role = result.user.role;
  token.tenantId = result.user.tenant_id;
  token.accessExpiresAt = Date.now() + ACCESS_SKEW_MS;
  token.mfaSetupRequired = result.mfa_setup_required === true;
  delete token.bridgeAuthError;
  return token;
}

export function signInAllowed({
  account,
  profile,
  user,
}: SignInCallbackParams): boolean {
  if (account?.provider === "oidc") return verifiedOidcClaims(profile) !== null;
  if (account?.provider === "cloudflare-access")
    return cfClaimsFromUser(user) !== null;
  if (account?.provider === "local") return localTokensFromUser(user) !== null;
  return false;
}

export async function bridgeJwtCallback({
  token,
  profile,
  account,
  user,
  trigger,
}: JwtCallbackParams): Promise<JWT> {
  // First sign-in: exchange only a verified, complete identity — OIDC profile
  // claims or a validated Cloudflare Access JWT. Throwing aborts Auth.js
  // sign-in instead of leaving a dashboard session half-created.
  if (account || trigger === "signIn" || trigger === "signUp") {
    // Local sign-in already holds bridge-issued tokens: authorize() got them
    // from POST /api/auth/login, which IS the exchange. Running a second one
    // here would need the dashboard↔bridge shared secret and an IdP issuer,
    // neither of which a local-only deployment has.
    if (account?.provider === "local") {
      const tokens = localTokensFromUser(user);
      if (!tokens) {
        throw new Error("verified identity required");
      }
      return applyLocalTokens(token, tokens);
    }
    const claims =
      account?.provider === "oidc"
        ? verifiedOidcClaims(profile)
        : account?.provider === "cloudflare-access"
          ? cfClaimsFromUser(user)
          : null;
    if (!claims) {
      throw new Error("verified identity required");
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
    delete session.mfaSetupRequired;
    delete session.bridgeRefresh;
    session.authError =
      (token.bridgeAuthError as BridgeAuthError | undefined) ??
      "BridgeSessionInvalid";
    return session;
  }

  session.bridgeAccess = bridgeAccess;
  // Server-only. `publicBridgeSessionCallback` deletes it before anything is
  // serialized to a browser — same treatment as bridgeAccess. The account
  // security route needs it so a password change can spare the caller's own
  // session instead of logging them out mid-panel.
  session.bridgeRefresh =
    typeof token.bridgeRefresh === "string" ? token.bridgeRefresh : undefined;
  session.role = token.role as string | undefined;
  session.tenantId = token.tenantId as string | undefined;
  // Not a credential — a policy flag. /api/auth/me stays authoritative; this
  // only lets the first render draw the banner without waiting on a fetch.
  session.mfaSetupRequired = token.mfaSetupRequired === true;
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
  delete internal.bridgeRefresh;
  return internal;
}

// A half-configured OIDC env (issuer without client id) must not produce a
// broken sign-in button or InvalidEndpoints noise. Shared with /signin.
export function oidcProviderConfigured(): boolean {
  return Boolean(
    OIDC_ISSUER?.trim() && process.env.AUTH_OIDC_CLIENT_ID?.trim(),
  );
}

// Providers register only when fully configured; the Cloudflare path stays off
// unless the deployment declares its team domain + application audience.
const providers: NextAuthConfig["providers"] = [];
if (oidcProviderConfigured()) {
  providers.push({
    id: "oidc",
    name: process.env.AUTH_OIDC_NAME || "SSO",
    type: "oidc",
    issuer: OIDC_ISSUER,
    clientId: process.env.AUTH_OIDC_CLIENT_ID,
    clientSecret: process.env.AUTH_OIDC_CLIENT_SECRET,
  });
}
// Registered UNCONDITIONALLY, with the enablement question moved inside
// authorize() where it can be asked of the bridge per attempt.
//
// It used to be `if (localLoginEnabled())`, evaluated once at module load from
// this process's environment — and that made the first-run wizard impossible to
// finish in one pass. The wizard turns local login on for the INSTANCE, in
// config.yaml, which the dashboard does not read; the provider array had
// already been built, so `signIn("local", …)` threw CredentialsSignin until
// someone restarted the dashboard. The Phase 1 exit criterion is "a clean
// container reaches chat in the Helm", and a flow whose happy path ends at a
// restart does not reach it.
//
// Registering the provider is not itself an authorisation: `authorize()` asks
// the bridge whether local login is offered and returns null when it is not,
// and the bridge 404s POST /api/auth/login regardless, so `bridgeLocalLogin`
// returns falsy on its own. Two refusals, and the load-bearing one is the
// bridge's.
providers.push(
  Credentials({
    id: "local",
    name: "Email and password",
    credentials: {
      email: { label: "Email", type: "email" },
      password: { label: "Password", type: "password" },
      code: { label: "One-time code", type: "text" },
    },
    async authorize(credentials, request) {
      const email =
        typeof credentials?.email === "string" ? credentials.email.trim() : "";
      const password =
        typeof credentials?.password === "string" ? credentials.password : "";
      const code =
        typeof credentials?.code === "string" ? credentials.code.trim() : "";
      if (!email || !password) return null;
      // The live answer, not the boot-time constant. Unreachable reads as off.
      if (!(await bridgeLocalLoginOffered(BRIDGE_URL()))) return null;

      const result = await bridgeLocalLogin(
        BRIDGE_URL(),
        { email, password, code },
        clientIpFromRequest(request),
      );
      // The bridge said the password was right and a code is owed. Throwing
      // is how Auth.js turns that into ?error=mfa_required rather than an
      // indistinguishable CredentialsSignin.
      if (result === "mfa_required") throw new MfaRequiredError();
      if (!result) return null;
      return {
        id: result.user.id,
        email: result.user.email,
        name: result.user.display_name,
        localTokens: result,
      };
    },
  }),
);
if (cfAccessEnabled()) {
  providers.push(
    Credentials({
      id: "cloudflare-access",
      name: "Cloudflare Access",
      credentials: {},
      async authorize(_credentials, request) {
        // The signIn() call is made server-side with the original incoming
        // headers, so the edge-injected assertion is read (and cryptographically
        // verified) here — a caller cannot fabricate a session without a JWT
        // signed by the team's JWKS.
        const assertion = request.headers.get(CF_JWT_HEADER);
        if (!assertion) return null;
        const claims = await verifyCfAccessJwt(assertion);
        if (!claims) return null;
        return {
          id: `${claims.issuer}|${claims.subject}`,
          email: claims.email,
          name: claims.display_name,
          cfClaims: claims,
        };
      },
    }),
  );
}

export const authConfig = {
  trustHost: true,
  pages: { signIn: "/signin" },
  session: { strategy: "jwt" },
  providers,
  callbacks: {
    signIn: signInAllowed,
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
