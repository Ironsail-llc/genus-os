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
  // Relative paths only: "/x" is fine, "//host/x" and absolute URLs are not.
  if (raw && raw.startsWith("/") && !raw.startsWith("//")) return raw;
  return "/";
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const callbackUrl = sanitizeCallbackUrl(request.nextUrl.searchParams.get("callbackUrl"));

  if (!cfAccessEnabled() || !request.headers.get(CF_JWT_HEADER)) {
    return NextResponse.redirect(
      new URL("/signin?error=CloudflareAccessUnavailable", request.url),
    );
  }

  try {
    const target: string = await signIn("cloudflare-access", {
      redirectTo: callbackUrl,
      redirect: false,
    });
    return NextResponse.redirect(new URL(target || callbackUrl, request.url));
  } catch {
    return NextResponse.redirect(new URL("/signin?error=CloudflareAccessFailed", request.url));
  }
}
