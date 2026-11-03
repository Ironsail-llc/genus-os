/**
 * Auth.js (next-auth v5) module augmentation.
 *
 * `bridgeAccess` exists only on the server-side Auth.js facade used by BFF
 * callers. The public `/api/auth/session` handler uses a separate callback that
 * deletes it before serialization. JWT-side fields stay loosely typed because
 * augmenting `next-auth/jwt` does not reach the v5 callback token parameter.
 */
import type { DefaultSession } from "next-auth";

import type { LocalLoginResult } from "@/lib/auth-local";
import type { CfVerifiedClaims } from "@/lib/cf-access";

declare module "next-auth" {
  interface Session {
    bridgeAccess?: string;
    // Server-only, like bridgeAccess: the public session callback deletes it
    // before anything reaches a browser.
    bridgeRefresh?: string;
    backendAuthorized?: boolean;
    role?: string;
    tenantId?: string;
    // The tenant the account belongs to. Differs from `tenantId` only while an
    // owner/admin acts in a descendant tenant. Non-secret.
    homeTenantId?: string;
    // The deployment's root tenant (`rootTenant()`). Non-secret; with the two
    // above it says whether the session is the root operator at home.
    rootTenantId?: string;
    // "acting-tenant-reverted" once a refresh could not re-enter the acting
    // tenant; cleared when the browser acknowledges it. Non-secret.
    tenantNotice?: "acting-tenant-reverted";
    // Owner accounts on a deployment whose ONLY sign-in method is local
    // email+password must enrol a second factor. Non-secret, so it may cross
    // into the browser session — the banner reads it there.
    mfaSetupRequired?: boolean;
    authError?: "BridgeRefreshFailed" | "BridgeSessionInvalid";
    user?: DefaultSession["user"] & { role?: string };
  }

  interface User {
    // Set by the cloudflare-access provider's authorize(); consumed by the
    // jwt callback for the bridge SSO exchange. Never serialized to clients.
    cfClaims?: CfVerifiedClaims;
    // Set by the local credentials provider's authorize(): the bridge already
    // minted the session, so the jwt callback applies these directly instead
    // of performing a second exchange. Never serialized to clients.
    localTokens?: LocalLoginResult;
  }
}
