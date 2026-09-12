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

const PUBLIC_PREFIXES = ["/signin", "/api/auth", "/setup", "/api/setup"];
const PUBLIC_PATHS = new Set(["/api/live", "/api/ready"]);

/**
 * How long a setup-status answer is reused. The gate below runs on EVERY
 * request that reaches the dashboard, so without a memo a busy page would put
 * one bridge round trip on the critical path of every asset and every API
 * call. Five seconds is short enough that the redirect stops within one page
 * load of the wizard finishing.
 */
const SETUP_STATUS_TTL_MS = 5_000;

let setupStatusCache: { at: number; incomplete: boolean } | null = null;

/** Test hook: forget the memoised answer. */
export function resetSetupStatusCache() {
  setupStatusCache = null;
}

/**
 * Whether this instance has NOT finished first-run setup.
 *
 * The bridge is the authority. `GET /api/setup/status` answers 200 while there
 * is no owner account and 404 once there is one — the whole setup router
 * disappears at that moment — so a 404 here means "complete", not "broken".
 *
 * Anything else (a connection refused, a 500, a timeout) is read as COMPLETE,
 * which is the direction that fails safe for a running appliance: an
 * unreachable bridge must not start sending signed-in operators to a first-run
 * wizard. It costs a fresh install nothing, because the page the operator
 * opens is `/setup?token=…`, which this gate never redirects away from.
 */
export async function setupIncomplete(
  bridgeUrl = process.env.BRIDGE_URL || "http://localhost:9100",
  now = Date.now(),
): Promise<boolean> {
  const cached = setupStatusCache;
  if (cached && now - cached.at < SETUP_STATUS_TTL_MS) return cached.incomplete;

  let incomplete = false;
  try {
    const res = await fetch(`${bridgeUrl}/api/setup/status`, {
      signal: AbortSignal.timeout(2_000),
      cache: "no-store",
    });
    if (res.ok) {
      const payload = (await res.json()) as { complete?: boolean } | null;
      incomplete = payload?.complete === false;
    }
  } catch {
    // See the docstring: unreachable reads as complete.
    incomplete = false;
  }
  setupStatusCache = { at: now, incomplete };
  return incomplete;
}

/**
 * The first-run redirect, as a pure function so it can be tested without a
 * network.
 *
 * While setup is incomplete every dashboard page goes to `/setup`: there is no
 * account yet, so the sign-in page it would otherwise land on renders nothing
 * the visitor can use. API routes are NOT redirected — a caller that asked for
 * JSON must not receive a page — and `/setup` plus `/api/setup/*` are left
 * alone or the redirect would chase its own tail.
 *
 * Once setup is complete `/setup` is 404, flatly. The bridge already refuses
 * every setup route at that point; this is the same answer one hop earlier, so
 * the wizard cannot even be rendered on a claimed appliance.
 */
export function setupRedirect(req: NextRequest, incomplete: boolean) {
  const { pathname } = req.nextUrl;
  const isSetupPage = pathname === "/setup" || pathname.startsWith("/setup/");
  const isSetupApi =
    pathname === "/api/setup" || pathname.startsWith("/api/setup/");

  if (!incomplete) {
    // A first-run page on an instance that has an owner is not "forbidden",
    // it does not exist.
    return isSetupPage ? new NextResponse("Not Found", { status: 404 }) : null;
  }
  if (isSetupPage || isSetupApi) return null;
  if (pathname.startsWith("/api/")) return null;

  const url = req.nextUrl.clone();
  url.pathname = "/setup";
  url.search = "";
  return NextResponse.redirect(url);
}

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
    PUBLIC_PREFIXES.some(
      (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
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
    return NextResponse.json(
      { error: "authentication required" },
      { status: 401 },
    );
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
export const proxy = auth(async (req) => {
  // First-run comes first: on an instance with no owner account there is
  // nothing for the session gate below to let anyone past to, and its answer
  // (a redirect to a sign-in page with no buttons on it) is the exact dead end
  // the wizard exists to remove.
  const firstRun = setupRedirect(req, await setupIncomplete());
  if (firstRun) return firstRun;
  return authorizeDashboardRequest(req, req.auth);
});

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
