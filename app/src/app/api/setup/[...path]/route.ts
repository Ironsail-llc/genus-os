/**
 * The wizard's own path to the bridge — same origin, and deliberately
 * session-free.
 *
 * The generic BFF proxy at `/api/bridge/[...path]` attaches THIS BROWSER'S
 * bridge access token to everything it forwards. Routing the first-run wizard
 * through it would mean the routes that create the owner account, store
 * provider credentials and install agents were reachable with a session, which
 * is the one thing that design refuses — so `bridge-proxy-policy.ts` denies
 * the `/api/setup` prefix there and the wizard comes through here instead.
 *
 * What this forwards, and nothing else:
 *
 * - the request body and the `Authorization` header, when it names a Bearer
 *   token. That is the setup CLAIM the wizard holds. No cookie, no session
 *   token, no other header from the caller.
 * - to a path under `/api/setup/` on the bridge, resolved through `new URL`
 *   and then re-checked, so `..` cannot walk out of the prefix and reach
 *   another route with the claim attached.
 *
 * The bridge refuses a session on those routes anyway, and refuses all of them
 * once an owner account exists. This is the near side of the same wall.
 */

import { NextRequest, NextResponse } from "next/server";

import { getServiceUrl } from "@/lib/services/registry";

const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";

/** The only prefix this route will forward to. */
const SETUP_PREFIX = "/api/setup/";

/** A body larger than the bridge's own credential-body ceiling. */
const MAX_BODY_BYTES = 8 * 1024;

async function forward(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path } = await context.params;
  const base = new URL(BRIDGE_URL);
  const target = new URL(
    `api/setup/${path.join("/").replace(/^\/+/, "")}`,
    base.origin + base.pathname.replace(/\/?$/, "/"),
  );
  // Re-checked AFTER resolution, which is when `..` segments have been
  // normalised away: the string that was safe to read is not necessarily the
  // string that will be requested.
  if (
    target.origin !== base.origin ||
    !target.pathname.startsWith(SETUP_PREFIX)
  ) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }
  target.search = req.nextUrl.search;

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  // The claim token, and only if it is shaped like one. A cookie is never
  // read here, so there is no path by which a session reaches the bridge
  // through this route.
  const authorization = req.headers.get("authorization") || "";
  if (/^Bearer\s+\S+$/i.test(authorization))
    headers.Authorization = authorization;

  let body: string | undefined;
  if (["POST", "PUT", "PATCH"].includes(req.method)) {
    body = await req.text();
    if (body.length > MAX_BODY_BYTES) {
      return NextResponse.json(
        { error: "Request body too large" },
        { status: 413 },
      );
    }
  }

  try {
    const res = await fetch(target.toString(), {
      method: req.method,
      headers,
      body,
      signal: AbortSignal.timeout(35_000),
      cache: "no-store",
    });
    const contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("json")) {
      return new NextResponse(await res.text(), { status: res.status });
    }
    return NextResponse.json(await res.json(), { status: res.status });
  } catch {
    return NextResponse.json(
      { error: "Bridge service unavailable" },
      { status: 502 },
    );
  }
}

export const GET = forward;
export const POST = forward;
