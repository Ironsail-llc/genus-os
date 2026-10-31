/**
 * Server-side Redis Streams reader for the Helm event bus.
 *
 * Uses ioredis to read from Robothor's Redis Streams.
 * Supports XREAD (no consumer group) for dashboard display,
 * and XREVRANGE for recent events.
 */

import Redis from "ioredis";

import type { Vital } from "@/lib/web-vitals";

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

export type RedisConnection =
  | { url: string }
  | { host: string; port: number; password?: string; db: number };

const DEFAULT_REDIS_URL = "redis://localhost:6379";

/**
 * Where the event bus lives, by the same rules as the Python services:
 * REDIS_URL wins when set; otherwise ROBOTHOR_REDIS_HOST/PORT/PASSWORD
 * (what the Helm chart supplies); otherwise the localhost default. Both of
 * the latter use ROBOTHOR_REDIS_DB (default 0), as RedisConfig does: an
 * engine writing DB N while the dashboard read DB 0 left the live views
 * empty with nothing logged.
 */
export function resolveRedisConnection(
  env: Record<string, string | undefined> = process.env
): RedisConnection {
  if (env.REDIS_URL) return { url: env.REDIS_URL };
  const parsedDb = Number.parseInt(env.ROBOTHOR_REDIS_DB ?? "", 10);
  const db = Number.isNaN(parsedDb) ? 0 : parsedDb;
  const host = env.ROBOTHOR_REDIS_HOST;
  if (!host) return { url: `${DEFAULT_REDIS_URL}/${db}` };
  const port = Number.parseInt(env.ROBOTHOR_REDIS_PORT ?? "", 10);
  const connection: RedisConnection = {
    host,
    port: Number.isNaN(port) ? 6379 : port,
    db,
  };
  if (env.ROBOTHOR_REDIS_PASSWORD) {
    connection.password = env.ROBOTHOR_REDIS_PASSWORD;
  }
  return connection;
}

let redisClient: Redis | null = null;

function getRedis(): Redis {
  if (!redisClient) {
    const connection = resolveRedisConnection();
    const options = { maxRetriesPerRequest: 3, lazyConnect: true };
    redisClient =
      "url" in connection
        ? new Redis(connection.url, options)
        : new Redis({ ...connection, ...options });
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

const VITALS_STREAM = `${STREAM_PREFIX}vitals`;
/** Roughly a day of a busy dashboard; XADD trims approximately, which is cheap. */
const VITALS_MAXLEN = "10000";

/**
 * Append one web-vitals metric to its own capped stream. Deliberately not one
 * of VALID_STREAMS: the live views have no use for it, and readers of the
 * event bus never see it.
 */
export async function writeVital(vital: Vital): Promise<void> {
  const fields: string[] = ["timestamp", new Date().toISOString()];
  for (const [key, value] of Object.entries(vital)) {
    if (value !== undefined) fields.push(key, String(value));
  }
  await getRedis().xadd(VITALS_STREAM, "MAXLEN", "~", VITALS_MAXLEN, "*", ...fields);
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
