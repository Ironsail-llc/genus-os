/**
 * Event bus stats endpoint.
 *
 * GET /api/events/stats
 *
 * Returns stream lengths for all event bus streams. The counts span every
 * tenant's events on the shared streams, so only a root session at home
 * (not acting in a child) may read them.
 */

import { auth } from "@/lib/auth";
import { streamLengths } from "@/lib/event-bus/redis-client";
import { rootTenant } from "@/lib/services/registry";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const session = await auth();
  if (!session?.bridgeAccess) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }
  const root = rootTenant();
  if (session.tenantId !== root || session.homeTenantId !== root) {
    return Response.json({ error: "Forbidden" }, { status: 403 });
  }
  try {
    const lengths = await streamLengths();
    const total = Object.values(lengths).reduce((sum, n) => sum + n, 0);
    return Response.json({
      streams: lengths,
      total,
      timestamp: new Date().toISOString(),
    });
  } catch {
    return Response.json(
      { error: "Event bus unavailable" },
      { status: 503 }
    );
  }
}
