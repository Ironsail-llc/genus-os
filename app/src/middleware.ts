/**
 * Edge gate — coarse "is there a session?" check that redirects to /signin.
 *
 * Deliberately a presence check (no JWT verify on Edge) — the authoritative
 * verification is the bridge JWT on every proxied backend call, and Cloudflare
 * Access remains in front as defense-in-depth. Keeping this Edge-light avoids
 * pulling the Node-only bridge-exchange logic (in @/lib/auth) into the Edge
 * runtime.
 *
 * Gated on GENUS_AUTH_ENFORCE (mirrors the bridge's shadow-mode flag): when
 * off/unset this is a pass-through, so a deployment without SSO configured
 * behaves exactly as before (no login wall). Flip it on in Phase B.
 */

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PUBLIC_PREFIXES = ["/signin", "/api/auth", "/api/health"];

// Auth.js session cookie names (dev + secure).
const SESSION_COOKIES = ["authjs.session-token", "__Secure-authjs.session-token"];

export function middleware(req: NextRequest) {
  // Shadow mode (default): don't gate anything until enforcement is enabled.
  if (process.env.GENUS_AUTH_ENFORCE !== "true") {
    return NextResponse.next();
  }
  const { pathname } = req.nextUrl;
  if (PUBLIC_PREFIXES.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }
  const hasSession = SESSION_COOKIES.some((c) => req.cookies.has(c));
  if (!hasSession) {
    const url = req.nextUrl.clone();
    url.pathname = "/signin";
    url.searchParams.set("callbackUrl", pathname);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  // Everything except Next internals + static assets.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)"],
};
