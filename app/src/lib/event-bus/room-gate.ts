/**
 * The events stream's second filter, after the tenant (`tenant-filter.ts`):
 * the rooms THIS caller can access (contract → Events: "filters by acting
 * tenant, then by the rooms the user can access").
 *
 * One gate per stream connection. The caller's room ids come from the bridge
 * (`GET /api/rooms`, the caller's own token), loaded the first time a room
 * event needs them and kept for `ttlMs`, so a revoked room stops arriving
 * within that time. A room id not in the list causes ONE reload — "you were
 * just given a workspace" — but never more often than `minRefreshMs`, so a
 * stream of unknown ids cannot turn into a stream of bridge requests. No list
 * (the bridge refused, failed or timed out) means no room events at all.
 *
 * Room events carry ids only. Anything else in a room event's payload is not
 * forwarded, so a future publisher that puts content in one cannot leak it
 * past the access check.
 */
import { isRoomEventType, roomIdOf } from "@/lib/rooms/events";

/** The payload fields a room event may carry to the browser. */
export const ROOM_EVENT_FIELDS = ["room_id", "tenant_id", "message_id", "edition_id", "task_id"] as const;

interface GateableEvent {
  type?: string;
  payload?: Record<string, unknown> | null;
}

export interface RoomGateOptions {
  /** The caller's room ids, or null when they could not be read. */
  loadRoomIds: () => Promise<Set<string> | null>;
  /** How long a list is trusted. */
  ttlMs?: number;
  /** The least time between two loads caused by unknown room ids. */
  minRefreshMs?: number;
  now?: () => number;
}

export function createRoomGate({ loadRoomIds, ttlMs = 60_000, minRefreshMs = 15_000, now = Date.now }: RoomGateOptions) {
  let ids: Set<string> | null = null;
  let loadedAt = -Infinity;
  let loaded = false;

  async function load() {
    loadedAt = now();
    loaded = true;
    try {
      ids = await loadRoomIds();
    } catch {
      ids = null;
    }
  }

  function strip<T extends GateableEvent>(event: T): T {
    const payload: Record<string, unknown> = {};
    for (const field of ROOM_EVENT_FIELDS) {
      if (event.payload && Object.hasOwn(event.payload, field)) payload[field] = event.payload[field];
    }
    return { ...event, payload };
  }

  async function filter<T extends GateableEvent>(events: T[]): Promise<T[]> {
    if (!events.some((e) => isRoomEventType(e.type))) return events;
    if (!loaded || now() - loadedAt >= ttlMs) await load();
    const unknown = events.some((e) => isRoomEventType(e.type) && !ids?.has(roomIdOf(e) ?? ""));
    if (unknown && now() - loadedAt >= minRefreshMs) await load();
    const out: T[] = [];
    for (const event of events) {
      if (!isRoomEventType(event.type)) out.push(event);
      else if (ids?.has(roomIdOf(event) ?? "")) out.push(strip(event));
    }
    return out;
  }

  return { filter };
}
