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
 * An `http(s)` origin, or null.
 *
 * A bare try/catch is NOT a guard here: `new URL("javascript:alert(1)")`
 * parses happily — protocol `javascript:`, origin the string `"null"` — and
 * `new URL("/signin", "null")` then throws outside the try, turning a bad env
 * var into a 500 on the sign-in path. Only http and https may become an origin.
 */
function httpOrigin(candidate: string | null | undefined): string | null {
  if (!candidate) return null;
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  return parsed.origin;
}

/**
 * The origin the BROWSER used, which is never the one `request.url` reports.
 *
 * Next's standalone server (`.next/standalone/server.js`) binds
 * `hostname = process.env.HOSTNAME || '0.0.0.0'` and attaches that to every
 * request (`attachRequestMeta`), so BOTH `request.url` and `request.nextUrl`
 * carry the BIND address unless `experimental.trustHostHeader` is set. Every
 * redirect derived from either pointed the operator at `https://0.0.0.0:3004/…`
 * ("0.0.0.0 refused to connect") the moment the app ran behind Cloudflare
 * Access, 2026-09-11.
 *
 * In order:
 *  1. `AUTH_URL` — Auth.js's own canonical public origin, and the only source
 *     that is configuration rather than inference. Set it.
 *  2. The proxy's forwarded headers. This is the SAME source Auth.js already
 *     trusts on this deployment (`trustHost: true` in lib/auth.ts), so it
 *     grants a spoofable header no trust that is not already granted — but it
 *     IS spoofable by anything that can reach the app without passing the
 *     proxy, which is why it ranks below configuration.
 *  3. `request.nextUrl.origin` — a last resort that is, in practice,
 *     unreachable: every HTTP/1.1 request carries a `Host`, so (2) answers
 *     first. It is here only so the function is total. It is NOT a safe
 *     default — on a fresh standalone install it is the bind address, i.e. the
 *     incident.
 *
 * The scheme, when the proxy does not state one, comes from the REQUEST and
 * not from a literal "https". Hardcoding https broke the one case (2) is
 * otherwise right about: `next dev` on http://localhost:3000 redirected to
 * https://localhost:3000, where nothing listens. Cloudflare always sets
 * `x-forwarded-proto`, so deriving it downgrades nothing in production.
 */
function publicOrigin(request: NextRequest): string {
  const configured = httpOrigin(process.env.AUTH_URL);
  if (configured) return configured;

  // A comma-separated header means a chain of proxies; the first entry is the
  // value the client-facing hop saw.
  const first = (value: string | null): string | null =>
    value ? (value.split(",")[0]?.trim() || null) : null;
  const host = first(request.headers.get("x-forwarded-host")) ?? first(request.headers.get("host"));
  if (host) {
    // `nextUrl.protocol` carries its trailing colon ("https:").
    const proto =
      first(request.headers.get("x-forwarded-proto")) ??
      request.nextUrl.protocol.replace(/:$/, "");
    const forwarded = httpOrigin(`${proto}://${host}`);
    if (forwarded) return forwarded;
  }

  return request.nextUrl.origin;
}

/**
 * Put `target` on `origin`, keeping only its path, query and fragment.
 *
 * `signIn(..., { redirect: false })` returns an ABSOLUTE URL, so
 * `new URL(target, origin)` would never rebase it — the base is ignored the
 * moment the input is absolute. Auth.js builds that URL from ITS base, which
 * with `AUTH_URL` unset it infers the same way everything else here did. So
 * the path is taken and the origin is imposed: this route's guarantee becomes
 * unconditional, and an absolute target can never send the browser off-origin.
 */
function onOrigin(target: string, origin: string): URL {
  const parsed = new URL(target, origin);
  return new URL(`${parsed.pathname}${parsed.search}${parsed.hash}`, origin);
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
    return NextResponse.redirect(onOrigin(target || callbackUrl, origin));
  } catch {
    return NextResponse.redirect(new URL("/signin?error=CloudflareAccessFailed", origin));
  }
}
