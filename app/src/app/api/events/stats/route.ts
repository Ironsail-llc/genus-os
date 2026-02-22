/**
 * Event bus stats endpoint.
 *
 * GET /api/events/stats
 *
 * Returns stream lengths for all event bus streams.
 */

import { streamLengths } from "@/lib/event-bus/redis-client";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
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
