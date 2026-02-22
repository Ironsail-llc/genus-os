/**
 * Server-side Redis Streams reader for the Helm event bus.
 *
 * Uses ioredis to read from Robothor's Redis Streams.
 * Supports XREAD (no consumer group) for dashboard display,
 * and XREVRANGE for recent events.
 */

import Redis from "ioredis";

const STREAM_PREFIX = "robothor:events:";

const VALID_STREAMS = [
  "email",
  "calendar",
  "crm",
  "vision",
  "health",
  "agent",
  "system",
] as const;

export type StreamName = (typeof VALID_STREAMS)[number];

export interface EventEnvelope {
  id: string;
  timestamp: string;
  type: string;
  source: string;
  actor: string;
  payload: Record<string, unknown>;
  correlation_id: string;
}

let redisClient: Redis | null = null;

function getRedis(): Redis {
  if (!redisClient) {
    const url = process.env.REDIS_URL || "redis://localhost:6379/0";
    redisClient = new Redis(url, {
      maxRetriesPerRequest: 3,
      lazyConnect: true,
    });
  }
  return redisClient;
}

export function streamKey(stream: StreamName): string {
  return `${STREAM_PREFIX}${stream}`;
}

function parseEntry(id: string, fields: string[]): EventEnvelope {
  const map: Record<string, string> = {};
  for (let i = 0; i < fields.length; i += 2) {
    map[fields[i]] = fields[i + 1];
  }
  let payload: Record<string, unknown> = {};
  try {
    payload = JSON.parse(map.payload || "{}");
  } catch {
    // leave empty
  }
  return {
    id,
    timestamp: map.timestamp || "",
    type: map.type || "",
    source: map.source || "",
    actor: map.actor || "",
    payload,
    correlation_id: map.correlation_id || "",
  };
}

/**
 * Read the N most recent events from a stream.
 */
export async function readRecent(
  stream: StreamName,
  count: number = 10
): Promise<EventEnvelope[]> {
  const redis = getRedis();
  const key = streamKey(stream);
  const entries = await redis.xrevrange(key, "+", "-", "COUNT", count);
  return entries.map(([id, fields]) => parseEntry(id, fields));
}

/**
 * Read events from multiple streams since given IDs.
 * Used for SSE polling — returns new events since last seen.
 *
 * @param lastIds Map of stream name to last seen message ID (or "0-0" for all)
 * @param blockMs How long to block waiting for new events
 * @returns Array of {stream, events} objects
 */
export async function readSince(
  lastIds: Partial<Record<StreamName, string>>,
  blockMs: number = 5000
): Promise<{ stream: StreamName; events: EventEnvelope[] }[]> {
  const redis = getRedis();
  const streams: string[] = [];
  const ids: string[] = [];

  for (const [stream, lastId] of Object.entries(lastIds)) {
    if (VALID_STREAMS.includes(stream as StreamName)) {
      streams.push(streamKey(stream as StreamName));
      ids.push(lastId || "$");
    }
  }

  if (streams.length === 0) return [];

  const result = await redis.xread(
    "COUNT",
    50,
    "BLOCK",
    blockMs,
    "STREAMS",
    ...streams,
    ...ids
  );

  if (!result) return [];

  return result.map(([key, entries]) => {
    const stream = key.replace(STREAM_PREFIX, "") as StreamName;
    return {
      stream,
      events: entries.map(([id, fields]) => parseEntry(id, fields)),
    };
  });
}

/**
 * Get stream lengths for all streams.
 */
export async function streamLengths(): Promise<
  Record<StreamName, number>
> {
  const redis = getRedis();
  const result = {} as Record<StreamName, number>;
  for (const stream of VALID_STREAMS) {
    try {
      result[stream] = await redis.xlen(streamKey(stream));
    } catch {
      result[stream] = 0;
    }
  }
  return result;
}

export function isValidStream(name: string): name is StreamName {
  return VALID_STREAMS.includes(name as StreamName);
}

export async function closeRedis(): Promise<void> {
  if (redisClient) {
    await redisClient.quit();
    redisClient = null;
  }
}
