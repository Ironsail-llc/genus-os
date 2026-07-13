/**
 * Dashboard session gate.
 *
 * Auth.js decrypts and verifies the session before this callback runs. A
 * cookie's presence is never treated as authentication, and a session is not
 * usable until the SSO exchange has produced a Bridge access token. Private
 * API routes return 401 instead of redirecting so callers never receive a
 * sign-in page as an apparently successful API response.
 *
 * Authentication is fail-closed by default. The only bypass is the explicit
 * development/test escape hatch; production can never enable it.
 */

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import type { Session } from "next-auth";

import { auth } from "@/lib/auth";

const PUBLIC_PREFIXES = ["/signin", "/api/auth"];
const PUBLIC_PATHS = new Set(["/api/live", "/api/ready"]);

function insecureDevelopmentMode(): boolean {
  const environment = (
    process.env.GENUS_ENVIRONMENT ??
    process.env.ROBOTHOR_ENVIRONMENT ??
    ""
  ).toLowerCase();
  return (
    process.env.GENUS_INSECURE_DEV_MODE === "true" &&
    environment !== "production" &&
    environment !== "prod"
  );
}

export function authorizeDashboardRequest(
  req: NextRequest,
  session: Session | null,
) {
  const { pathname } = req.nextUrl;
  if (
    PUBLIC_PATHS.has(pathname) ||
    PUBLIC_PREFIXES.some((prefix) =>
      pathname === prefix || pathname.startsWith(`${prefix}/`)
    )
  ) {
    return NextResponse.next();
  }

  if (insecureDevelopmentMode()) {
    return NextResponse.next();
  }

  if (session?.user && session.bridgeAccess && !session.authError) {
    return NextResponse.next();
  }

  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "authentication required" }, { status: 401 });
  }

  const url = req.nextUrl.clone();
  url.pathname = "/signin";
  url.searchParams.set("callbackUrl", `${pathname}${req.nextUrl.search}`);
  return NextResponse.redirect(url);
}

// The wrapper validates/decrypts the Auth.js JWT and exposes it as `req.auth`.
// Keep authorization logic in the pure function above so the policy has direct
// regression coverage without replacing cryptographic verification with a
// cookie-presence mock.
export const proxy = auth((req) => authorizeDashboardRequest(req, req.auth));

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)"],
};
