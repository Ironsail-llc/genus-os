/**
 * Auth.js (next-auth v5) module augmentation.
 *
 * `bridgeAccess` exists only on the server-side Auth.js facade used by BFF
 * callers. The public `/api/auth/session` handler uses a separate callback that
 * deletes it before serialization. JWT-side fields stay loosely typed because
 * augmenting `next-auth/jwt` does not reach the v5 callback token parameter.
 */
import type { DefaultSession } from "next-auth";

import type { CfVerifiedClaims } from "@/lib/cf-access";

declare module "next-auth" {
  interface Session {
    bridgeAccess?: string;
    backendAuthorized?: boolean;
    role?: string;
    tenantId?: string;
    authError?: "BridgeRefreshFailed" | "BridgeSessionInvalid";
    user?: DefaultSession["user"] & { role?: string };
  }

  interface User {
    // Set by the cloudflare-access provider's authorize(); consumed by the
    // jwt callback for the bridge SSO exchange. Never serialized to clients.
    cfClaims?: CfVerifiedClaims;
  }
}
