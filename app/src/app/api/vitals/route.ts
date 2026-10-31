/**
 * Web-vitals sink.
 *
 * POST /api/vitals — one metric per request, sent by the reporter in the root
 * layout with navigator.sendBeacon. Each valid metric is logged as one JSON
 * line (`"event":"web_vital"`) for the log pipeline and appended to the
 * capped Redis stream `robothor:events:vitals`. The log line is written first:
 * a Redis outage costs the stream entry, never the measurement or the page.
 *
 * GENUS_DASHBOARD_WEB_VITALS=false turns the sink off (204, nothing written).
 */

import { ALL_VIEW_IDS } from "@/components/layout/nav-config";
import { writeVital } from "@/lib/event-bus/redis-client";
import { serverLog } from "@/lib/server-log";
import { parseVital, webVitalsEnabled } from "@/lib/web-vitals";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const NO_CONTENT = () => new Response(null, { status: 204 });

export async function POST(req: Request) {
  if (!webVitalsEnabled()) return NO_CONTENT();

  let body: unknown;
  try {
    body = JSON.parse(await req.text());
  } catch {
    return Response.json({ error: "Body must be JSON" }, { status: 400 });
  }
  const vital = parseVital(body);
  if (!vital) return Response.json({ error: "Not a web-vitals metric" }, { status: 400 });
  // Only views the dashboard has, so the field never takes arbitrary values.
  if (vital.view && !(ALL_VIEW_IDS as readonly string[]).includes(vital.view)) delete vital.view;

  serverLog({ event: "web_vital", ...vital });
  try {
    await writeVital(vital);
  } catch {
    // The log line above is the record of last resort.
  }
  return NO_CONTENT();
}
