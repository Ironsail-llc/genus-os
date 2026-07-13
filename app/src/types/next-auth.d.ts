/**
 * Auth.js (next-auth v5) module augmentation.
 *
 * Declares the bridge-issued fields we expose on the Session so server-side
 * callers (the proxies via @/lib/bridge-auth) can read `session.bridgeAccess`
 * without an `as Record<string, unknown>` cast. The JWT-side fields stay
 * loosely typed (read with explicit casts in the auth callbacks) — augmenting
 * `next-auth/jwt`'s `JWT` doesn't reach the callback `token` param in v5 beta.
 */
import type { DefaultSession } from "next-auth";

declare module "next-auth" {
  interface Session {
    bridgeAccess?: string;
    role?: string;
    tenantId?: string;
    user?: DefaultSession["user"] & { role?: string };
  }
}
