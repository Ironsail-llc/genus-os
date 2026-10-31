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
 *
 * Public, so a signed-out page (sign-in) reports its vitals too: the proxy
 * lets this one path through, and the route bounds what that exposes -- a
 * per-client rate limit (429), a body cap (413) and the strict schema below.
 * Nothing it accepts is ever read back by a page.
 */

import { ALL_VIEW_IDS } from "@/components/layout/nav-config";
import { ActionRateLimiter } from "@/lib/action-rate-limit";
import { clientIpFromRequest } from "@/lib/auth-local";
import { writeVital } from "@/lib/event-bus/redis-client";
import { serverLog } from "@/lib/server-log";
import {
  parseVital,
  VITALS_MAX_BODY_BYTES,
  VITALS_RATE_LIMIT,
  VITALS_RATE_WINDOW_MS,
  webVitalsEnabled,
} from "@/lib/web-vitals";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const NO_CONTENT = () => new Response(null, { status: 204 });
const TOO_LARGE = () => Response.json({ error: "Body too large" }, { status: 413 });

const limiter = new ActionRateLimiter(VITALS_RATE_LIMIT, VITALS_RATE_WINDOW_MS);

export async function POST(req: Request) {
  if (!webVitalsEnabled()) return NO_CONTENT();

  // The verified client address, or one shared key when none can be verified.
  const client = clientIpFromRequest(req) ?? "unattributed";
  if (!limiter.allow(client)) {
    return Response.json(
      { error: "Too many vitals" },
      { status: 429, headers: { "Retry-After": String(limiter.retryAfterSeconds(client)) } }
    );
  }

  if (Number(req.headers.get("content-length") ?? 0) > VITALS_MAX_BODY_BYTES) return TOO_LARGE();
  const text = await req.text();
  if (text.length > VITALS_MAX_BODY_BYTES) return TOO_LARGE();

  let body: unknown;
  try {
    body = JSON.parse(text);
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
