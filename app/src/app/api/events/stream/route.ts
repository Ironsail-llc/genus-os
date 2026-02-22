/**
 * SSE endpoint for Helm live event stream.
 *
 * GET /api/events/stream?streams=email,crm,health
 *
 * Streams real-time events from Redis Streams to the browser.
 * Uses XREAD with BLOCK for efficient polling.
 */

import { NextRequest } from "next/server";
import {
  readRecent,
  readSince,
  isValidStream,
  type StreamName,
  type EventEnvelope,
} from "@/lib/event-bus/redis-client";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
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
  const stream = new ReadableStream({
    async start(controller) {
      try {
        // Send recent events first (last 20 per stream)
        for (const streamName of requestedStreams) {
          try {
            const recent = await readRecent(streamName, 20);
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

        // Track last IDs per stream
        const lastIds: Partial<Record<StreamName, string>> = {};
        for (const s of requestedStreams) {
          lastIds[s] = "$"; // Only new events from now
        }

        // Long-poll loop
        let iterations = 0;
        const MAX_ITERATIONS = 600; // ~50 minutes at 5s blocks
        while (iterations < MAX_ITERATIONS) {
          iterations++;
          try {
            const results = await readSince(lastIds, 5000);
            for (const { stream: streamName, events } of results) {
              for (const event of events) {
                const data = JSON.stringify({
                  stream: streamName,
                  ...event,
                });
                controller.enqueue(
                  encoder.encode(`event: message\ndata: ${data}\n\n`)
                );
                lastIds[streamName] = event.id;
              }
            }

            // Heartbeat every 30s
            if (iterations % 6 === 0) {
              controller.enqueue(
                encoder.encode(`event: heartbeat\ndata: {"ts":"${new Date().toISOString()}"}\n\n`)
              );
            }
          } catch (error) {
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
        controller.close();
      }
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
