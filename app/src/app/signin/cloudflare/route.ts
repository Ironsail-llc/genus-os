/**
 * Cloudflare Access sign-in handler.
 *
 * /signin auto-redirects here when the deployment trusts Cloudflare Access.
 * signIn() must run in a route handler (it sets the session cookie, which a
 * server-component render is not allowed to do); the provider's authorize()
 * verifies the edge-injected `Cf-Access-Jwt-Assertion` from these same request
 * headers against the team JWKS. Every failure lands back on /signin?error=…,
 * which suppresses the auto-redirect — no loop.
 */

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { signIn } from "@/lib/auth";
import { CF_JWT_HEADER, cfAccessEnabled } from "@/lib/cf-access";

export const dynamic = "force-dynamic";

function sanitizeCallbackUrl(raw: string | null): string {
  // Relative paths only: "/x" is fine; "//host", absolute URLs, and anything
  // containing "\" are not (WHATWG URL parsing folds "/\" into "//", so a
  // backslash would reopen the protocol-relative redirect).
  if (raw && raw.startsWith("/") && !raw.startsWith("//") && !raw.includes("\\")) return raw;
  return "/";
}

/**
 * The origin the BROWSER used, which is not the one `request.url` reports.
 *
 * Next's standalone server (`.next/standalone/server.js`) binds
 * `hostname = process.env.HOSTNAME || '0.0.0.0'`, and a route handler's
 * `request.url` is built from that bind address — so every redirect derived
 * from it pointed the operator at `https://0.0.0.0:3004/...` ("0.0.0.0 refused
 * to connect") the moment the app ran behind Cloudflare Access, 2026-09-11.
 * `AUTH_URL` is Auth.js's own canonical public origin and is already set in
 * production; `request.nextUrl.origin` is the dev/test fallback.
 */
function publicOrigin(request: NextRequest): string {
  const configured = process.env.AUTH_URL;
  if (configured) {
    try {
      return new URL(configured).origin;
    } catch {
      // Not absolute — unusable as an origin; fall through.
    }
  }
  return request.nextUrl.origin;
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const callbackUrl = sanitizeCallbackUrl(request.nextUrl.searchParams.get("callbackUrl"));
  const origin = publicOrigin(request);

  if (!cfAccessEnabled() || !request.headers.get(CF_JWT_HEADER)) {
    return NextResponse.redirect(new URL("/signin?error=CloudflareAccessUnavailable", origin));
  }

  try {
    const target: string = await signIn("cloudflare-access", {
      redirectTo: callbackUrl,
      redirect: false,
    });
    return NextResponse.redirect(new URL(target || callbackUrl, origin));
  } catch {
    return NextResponse.redirect(new URL("/signin?error=CloudflareAccessFailed", origin));
  }
}
