/**
 * SSE endpoint for Helm live event stream.
 *
 * GET /api/events/stream?streams=email,crm,health
 *
 * Streams real-time events from Redis Streams to the browser.
 * Uses XREAD with BLOCK for efficient polling, on a Redis connection of its
 * own per request (a blocking read occupies the connection it runs on).
 */

import { NextRequest } from "next/server";
import { auth } from "@/lib/auth";
import {
  readRecent,
  openStreamReader,
  isValidStream,
  type StreamName,
  type StreamReader,
} from "@/lib/event-bus/redis-client";
import { visibleToTenant } from "@/lib/event-bus/tenant-filter";
import { createRoomGate } from "@/lib/event-bus/room-gate";
import { getServiceUrl } from "@/lib/services/registry";
import { decodeJwt } from "jose";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Credentials for the bridge, fixed when the stream is OPENED.
 *
 * A stream lives far longer than its request (up to ~50 minutes). Looking the
 * session up again from inside it (`auth()`, `bridgeAuthHeaders()`) would run
 * the jwt callback against the cookie from connect time, and past its skew
 * that callback spends the single-use bridge refresh token on a rotation that
 * can never be written back: the stream's headers are long sent. So the bearer
 * is taken once, in `GET`, and the stream closes before it expires (or on a
 * bridge 401). `EventSource` then reconnects through the proxy, which
 * refreshes the cookie the ordinary way.
 */
type BridgeCaller = {
  headers: Record<string, string>;
  /** The bridge refused the token: end the stream so the browser reconnects. */
  onUnauthorized: () => void;
};

/** Close this long before the access token's `exp`. */
const EXPIRY_MARGIN_MS = 30_000;
/**
 * When the token carries no readable `exp`: the proxy refreshes any token
 * past its 14-minute skew before the request reaches here, so at least this
 * much of its 15-minute life is left.
 */
const UNKNOWN_EXPIRY_LIFETIME_MS = 60_000;

/** When the stream must close, from the bearer it was opened with. */
function closeAt(bearer: string, now = Date.now()): number {
  try {
    const exp = decodeJwt(bearer).exp;
    if (typeof exp === "number") return exp * 1000 - EXPIRY_MARGIN_MS;
  } catch {
    // Not a JWT we can read: the conservative bound below.
  }
  return now + UNKNOWN_EXPIRY_LIFETIME_MS;
}

function bridgeBase(): string {
  return (getServiceUrl("bridge") || "http://localhost:9100").replace(/\/$/, "");
}

/**
 * The caller's room ids, from the bridge with the caller's own token; null
 * when they cannot be read, which the gate treats as "no room events".
 */
async function callerRoomIds(caller: BridgeCaller): Promise<Set<string> | null> {
  try {
    const res = await fetch(`${bridgeBase()}/api/rooms`, {
      headers: caller.headers,
      signal: AbortSignal.timeout(10_000),
      cache: "no-store",
    });
    if (res.status === 401) caller.onUnauthorized();
    if (!res.ok) return null;
    const data = (await res.json()) as { rooms?: Array<{ id?: unknown }> };
    if (!Array.isArray(data?.rooms)) return null;
    return new Set(data.rooms.map((room) => room?.id).filter((id): id is string => typeof id === "string"));
  } catch {
    return null;
  }
}

/**
 * Whether the caller can open one room outside their list (a tenant admin who
 * is not a member): the bridge answers `GET /api/rooms/{id}` 200, with the
 * caller's own token. Anything else, including a failure, is a "no". The gate
 * passes only well-formed room ids.
 */
async function callerCanOpenRoom(caller: BridgeCaller, roomId: string): Promise<boolean> {
  try {
    const res = await fetch(`${bridgeBase()}/api/rooms/${encodeURIComponent(roomId)}`, {
      headers: caller.headers,
      signal: AbortSignal.timeout(10_000),
      cache: "no-store",
    });
    if (res.status === 401) caller.onUnauthorized();
    return res.ok;
  } catch {
    return false;
  }
}

export async function GET(request: NextRequest) {
  // The streams are shared by every tenant's engine and the bridge; this
  // session sees only the events of the tenant it is acting in.
  const session = await auth();
  if (!session?.bridgeAccess || !session.tenantId) {
    return new Response("Unauthorized", { status: 401 });
  }
  const tenant = session.tenantId;
  const bearer = session.bridgeAccess;

  const streamsParam =
    request.nextUrl.searchParams.get("streams") || "email,crm,health,agent";
  const requestedStreams = streamsParam
    .split(",")
    .filter(isValidStream) as StreamName[];

  if (requestedStreams.length === 0) {
    return new Response("No valid streams specified", { status: 400 });
  }

  // Send initial recent events, then stream new ones
  const encoder = new TextEncoder();
  let reader: StreamReader | null = null;
  let cancelled = false;
  const stop = () => {
    cancelled = true;
    const r = reader;
    reader = null;
    void r?.close();
  };
  request.signal?.addEventListener("abort", stop);

  // Then the rooms this caller can access: one gate per connection, asking
  // the bridge with the token this request arrived with and nothing later.
  const caller: BridgeCaller = { headers: { Authorization: `Bearer ${bearer}` }, onUnauthorized: stop };
  const rooms = createRoomGate({
    loadRoomIds: () => callerRoomIds(caller),
    checkRoom: (roomId) => callerCanOpenRoom(caller, roomId),
  });
  // And no longer than that token lives.
  const expiryTimer = setTimeout(stop, Math.max(0, closeAt(bearer) - Date.now()));

  const stream = new ReadableStream({
    async start(controller) {
      // Where each stream's live read starts: the newest event replayed below,
      // so an event written between the replay and the first XREAD (or
      // between two XREADs) is still delivered. "$" would skip it.
      const lastIds: Partial<Record<StreamName, string>> = {};
      try {
        // Send recent events first (last 20 per stream)
        for (const streamName of requestedStreams) {
          // Validate the key against the allow-list before using it as an
          // object property to block remote property injection.
          if (!isValidStream(streamName)) continue;
          lastIds[streamName] = "$";
          try {
            const raw = await readRecent(streamName, 20);
            const recent = await rooms.filter(visibleToTenant(raw, tenant));
            // XREVRANGE is newest first; an empty stream starts from the beginning.
            // The cursor is the newest id this read covered, including events
            // filtered out as another tenant's, so the live read resumes after them.
            lastIds[streamName] = raw[0]?.id ?? "0-0";
            for (const event of recent.reverse()) {
              const data = JSON.stringify({
                stream: streamName,
                ...event,
              });
              controller.enqueue(
                encoder.encode(`event: message\ndata: ${data}\n\n`)
              );
            }
          } catch {
            // Skip streams that don't exist yet
          }
        }

        // Send initial sync complete marker
        controller.enqueue(
          encoder.encode(`event: sync\ndata: {"status":"synced"}\n\n`)
        );

        if (cancelled) return;
        reader = openStreamReader();

        // Long-poll loop
        let iterations = 0;
        const MAX_ITERATIONS = 600; // ~50 minutes at 5s blocks
        while (iterations < MAX_ITERATIONS && !cancelled && reader) {
          iterations++;
          try {
            const results = await reader.readSince(lastIds, 5000);
            if (cancelled) break;
            for (const { stream: streamName, events } of results) {
              for (const event of events) {
                // Advance past every event, send only this tenant's.
                lastIds[streamName] = event.id;
              }
              for (const event of await rooms.filter(visibleToTenant(events, tenant))) {
                const data = JSON.stringify({
                  stream: streamName,
                  ...event,
                });
                controller.enqueue(
                  encoder.encode(`event: message\ndata: ${data}\n\n`)
                );
              }
            }

            // Heartbeat every 30s
            if (iterations % 6 === 0) {
              controller.enqueue(
                encoder.encode(`event: heartbeat\ndata: {"ts":"${new Date().toISOString()}"}\n\n`)
              );
            }
          } catch {
            if (cancelled) break; // The reader was closed under a pending read
            // Redis connection error — send error and close
            controller.enqueue(
              encoder.encode(
                `event: error\ndata: {"error":"Redis connection lost"}\n\n`
              )
            );
            break;
          }
        }
      } catch {
        // Stream setup error
      } finally {
        request.signal?.removeEventListener("abort", stop);
        clearTimeout(expiryTimer);
        stop();
        try {
          controller.close();
        } catch {
          // Already closed by the client
        }
      }
    },
    cancel() {
      clearTimeout(expiryTimer);
      stop();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
