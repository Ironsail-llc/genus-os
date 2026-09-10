import { NextRequest, NextResponse } from "next/server";

import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { getServiceUrl } from "@/lib/services/registry";
const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";

/**
 * Bridge paths this proxy will not forward, whatever the method.
 *
 * This route hands the caller's browser session to ANY bridge path, so
 * `/api/vault/get` — which used to answer an owner/admin session with a
 * decrypted credential — was reachable from the Helm, from an XSS on it, and
 * from anything holding a session cookie. The bridge now refuses human
 * sessions on that route; the browser still has no business asking, so it is
 * refused here as well. Two independent locks, because one of them was enough
 * to leak every secret in the appliance.
 *
 * Matched against the RESOLVED target path (after `new URL` normalizes any
 * `..` segments), never the raw string the caller supplied.
 */
const DENIED_BRIDGE_PATHS = /^\/(?:api\/)?vault\/(?:get|list)(?:\/|$)/i;

async function proxy(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> }
) {
  const { path } = await context.params;
  const base = new URL(BRIDGE_URL);
  const pathStr = path.join("/");
  const target = new URL(
    pathStr.replace(/^\/+/, ""),
    base.origin + base.pathname.replace(/\/?$/, "/")
  );
  if (target.origin !== base.origin) {
    return new NextResponse("Bad gateway path", { status: 502 });
  }
  if (DENIED_BRIDGE_PATHS.test(target.pathname)) {
    // 404, not 403: the browser is told this proxy has no such route, rather
    // than that there is a secret here it may not have.
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }
  target.search = req.nextUrl.search;

  try {
    const headers: Record<string, string> = {
      "Content-Type": req.headers.get("content-type") || "application/json",
      ...(await bridgeAuthHeaders()),
    };

    const res = await fetch(target.toString(), {
      method: req.method,
      headers,
      body: ["POST", "PUT", "PATCH"].includes(req.method)
        ? await req.text()
        : undefined,
      signal: AbortSignal.timeout(30000),
    });

    const contentType = res.headers.get("content-type") || "";
    const body = contentType.includes("json")
      ? await res.json()
      : await res.text();

    return contentType.includes("json")
      ? NextResponse.json(body, { status: res.status })
      : new NextResponse(body as string, { status: res.status });
  } catch {
    return NextResponse.json(
      { error: "Bridge service unavailable" },
      { status: 502 }
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
