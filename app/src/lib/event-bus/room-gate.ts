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
 * The list holds the caller's OWN rooms. A tenant admin can open a room they
 * are not a member of, so a room id the list does not hold is also checked on
 * its own (`checkRoom`: `GET /api/rooms/{id}` with the caller's token, 200 or
 * not). Each answer is kept like the list, and no more than
 * `maxChecksPerWindow` checks run in any `minRefreshMs`; a check that fails
 * is a "no".
 *
 * Room events carry ids only. Anything else in a room event's payload is not
 * forwarded, so a future publisher that puts content in one cannot leak it
 * past the access check.
 */
import { isRoomEventType, roomIdOf } from "@/lib/rooms/events";
import { isRoomId } from "@/lib/rooms/room-id";

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
  /** Whether the caller can open one room that is not in their list. */
  checkRoom?: (roomId: string) => Promise<boolean>;
  /** How many single-room checks may run in any `minRefreshMs`. */
  maxChecksPerWindow?: number;
  now?: () => number;
}

export function createRoomGate({
  loadRoomIds,
  ttlMs = 60_000,
  minRefreshMs = 15_000,
  checkRoom,
  maxChecksPerWindow = 5,
  now = Date.now,
}: RoomGateOptions) {
  let ids: Set<string> | null = null;
  let loadedAt = -Infinity;
  let loaded = false;
  const checked = new Map<string, { ok: boolean; at: number }>();
  let windowStart = -Infinity;
  let checksInWindow = 0;

  /** Whether the caller can open `roomId` though their list lacks it. */
  async function admitted(roomId: string | null): Promise<boolean> {
    if (!checkRoom || !roomId || !isRoomId(roomId) || ids === null) return false;
    const known = checked.get(roomId);
    if (known && now() - known.at < ttlMs) return known.ok;
    if (now() - windowStart >= minRefreshMs) {
      windowStart = now();
      checksInWindow = 0;
    }
    if (checksInWindow >= maxChecksPerWindow) return false;
    checksInWindow++;
    let ok = false;
    try {
      ok = (await checkRoom(roomId)) === true;
    } catch {
      ok = false;
    }
    checked.set(roomId, { ok, at: now() });
    if (checked.size > 500) checked.clear();
    return ok;
  }

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
      const roomId = roomIdOf(event);
      if (!isRoomEventType(event.type)) out.push(event);
      else if (ids?.has(roomId ?? "") || (await admitted(roomId))) out.push(strip(event));
    }
    return out;
  }

  return { filter };
}
