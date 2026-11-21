/**
 * The browser's reads and writes on the rooms Bridge API, through the BFF
 * bridge proxy (`/api/bridge/[...path]`), which attaches the caller's own
 * bridge credentials. A refusal or a lost request throws `FetchRefused`, so a
 * hook reports `{ endpoint, status }` instead of showing an empty list.
 */
import { FetchRefused } from "@/lib/query/fetch-refused";
import type { Edition, Room, RoomSummary } from "./types";

export const ROOMS_ENDPOINT = "/api/bridge/api/rooms";

export function roomEndpoint(roomId: string, rest = ""): string {
  return `${ROOMS_ENDPOINT}/${encodeURIComponent(roomId)}${rest}`;
}

async function getJson<T>(endpoint: string, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(endpoint, { signal, cache: "no-store" });
  } catch (err) {
    if (signal?.aborted) throw err;
    throw new FetchRefused(endpoint, 0);
  }
  if (!res.ok) throw new FetchRefused(endpoint, res.status);
  try {
    return (await res.json()) as T;
  } catch (err) {
    if (signal?.aborted) throw err;
    throw new FetchRefused(endpoint, 0);
  }
}

export async function fetchRooms(signal?: AbortSignal): Promise<RoomSummary[]> {
  const data = await getJson<{ rooms?: unknown }>(ROOMS_ENDPOINT, signal);
  return Array.isArray(data?.rooms) ? (data.rooms as RoomSummary[]) : [];
}

/** A room the caller cannot see answers 404 (never 403): `FetchRefused` with that status. */
export function fetchRoom(roomId: string, signal?: AbortSignal): Promise<Room> {
  return getJson<Room>(roomEndpoint(roomId), signal);
}

/**
 * A room's editions, newest first. The contract names the route, not the
 * envelope: `{editions: [...]}` (like `{rooms: [...]}`) or a bare list.
 */
export async function fetchEditions(roomId: string, signal?: AbortSignal): Promise<Edition[]> {
  const data = await getJson<unknown>(roomEndpoint(roomId, "/editions"), signal);
  const rows = Array.isArray(data)
    ? data
    : Array.isArray((data as { editions?: unknown })?.editions)
      ? ((data as { editions: unknown[] }).editions)
      : [];
  return (rows as Edition[]).slice().sort((a, b) => (b.seq ?? 0) - (a.seq ?? 0));
}

export function fetchEdition(roomId: string, editionId: string, signal?: AbortSignal): Promise<Edition> {
  return getJson<Edition>(roomEndpoint(roomId, `/editions/${encodeURIComponent(editionId)}`), signal);
}

/** `POST /api/rooms/{id}/read`. True when the bridge took it. */
export async function postRoomRead(roomId: string): Promise<boolean> {
  try {
    const res = await fetch(roomEndpoint(roomId, "/read"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    return res.ok;
  } catch {
    return false;
  }
}
