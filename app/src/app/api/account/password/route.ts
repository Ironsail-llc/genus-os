import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { auth } from "@/lib/auth";
import { getServiceUrl } from "@/lib/services/registry";

/**
 * Password change, routed HERE rather than through the generic `/api/bridge`
 * proxy for one reason: the bridge revokes every other refresh session when a
 * password changes, and to spare the caller's own it needs that session's
 * refresh token. Only a server-side route with the Auth.js session facade can
 * supply it — the browser never holds it, and the generic proxy forwards only
 * the access token.
 *
 * The refresh token therefore travels dashboard → bridge on the internal
 * network, the same channel `/api/auth/refresh` already uses, and the bridge
 * compares only its hash. It never reaches the browser in either direction.
 */
const BRIDGE_URL = () => getServiceUrl("bridge") || "http://localhost:9100";

export async function POST(req: NextRequest) {
  const session = await auth();
  if (!session?.bridgeAccess) {
    return NextResponse.json({ error: "authentication required" }, { status: 401 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid request" }, { status: 422 });
  }
  const payload = body as { current_password?: unknown; new_password?: unknown } | null;
  if (
    typeof payload?.current_password !== "string" ||
    typeof payload?.new_password !== "string"
  ) {
    return NextResponse.json({ error: "invalid request" }, { status: 422 });
  }

  try {
    const res = await fetch(`${BRIDGE_URL()}/api/auth/password`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${session.bridgeAccess}`,
      },
      body: JSON.stringify({
        current_password: payload.current_password,
        new_password: payload.new_password,
        ...(session.bridgeRefresh ? { keep_refresh_token: session.bridgeRefresh } : {}),
      }),
      signal: AbortSignal.timeout(30000),
    });
    // The bridge's body is already free of any submitted credential (its
    // credential routes answer with fixed strings), so it is safe to relay.
    const relayed: unknown = await res.json().catch(() => ({ error: "invalid credentials" }));
    return NextResponse.json(relayed, { status: res.status });
  } catch {
    return NextResponse.json({ error: "Bridge service unavailable" }, { status: 502 });
  }
}
