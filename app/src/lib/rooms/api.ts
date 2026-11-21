/**
 * The browser's reads and writes on the rooms Bridge API, through the BFF
 * bridge proxy (`/api/bridge/[...path]`), which attaches the caller's own
 * bridge credentials. A refusal or a lost request throws `FetchRefused`, so a
 * hook reports `{ endpoint, status }` instead of showing an empty list.
 */
import { FetchRefused } from "@/lib/query/fetch-refused";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import type { RoomTask } from "./hub";
import type { Edition, Room, RoomKind, RoomSummary } from "./types";

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

// ── Hub (lane 11): a room's tasks, creating rooms, opening a task as one ──

/** A write the bridge took, or why it did not (`status` 0: the request was lost). */
export type RoomWrite<T> =
  | { ok: true; value: T }
  | { ok: false; status: number; message: string; body: unknown };

async function postJson<T>(endpoint: string, body?: unknown): Promise<RoomWrite<T>> {
  let res: Response;
  try {
    res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    return { ok: false, status: 0, message: "The dashboard could not reach the server.", body: null };
  }
  if (!res.ok) {
    let parsed: unknown = null;
    try {
      parsed = await res.clone().json();
    } catch {
      parsed = null;
    }
    // `{detail: {error, message, ...}}` carries its sentence one level down,
    // where the shared reader does not look.
    const wrapped = refusalFields(parsed).message;
    const message = typeof wrapped === "string" && wrapped.trim() ? wrapped : await readBridgeReply(res);
    return { ok: false, status: res.status, message, body: parsed };
  }
  try {
    return { ok: true, value: (await res.json()) as T };
  } catch {
    return { ok: true, value: null as T };
  }
}

/** `GET /api/rooms/{id}/tasks`: `{tasks: [...]}` or a bare list, as the editions route. */
export async function fetchRoomTasks(roomId: string, signal?: AbortSignal): Promise<RoomTask[]> {
  const data = await getJson<unknown>(roomEndpoint(roomId, "/tasks"), signal);
  if (Array.isArray(data)) return data as RoomTask[];
  const rows = (data as { tasks?: unknown })?.tasks;
  return Array.isArray(rows) ? (rows as RoomTask[]) : [];
}

export type TaskDecision =
  | { ok: true; task: RoomTask | null }
  | { ok: false; status: number; message: string; takenBy: string | null };

/** A refusal's own fields: at the top level, or inside FastAPI's `detail` wrapper. */
function refusalFields(body: unknown): Record<string, unknown> {
  if (!body || typeof body !== "object") return {};
  const detail = (body as { detail?: unknown }).detail;
  return detail && typeof detail === "object" && !Array.isArray(detail)
    ? (detail as Record<string, unknown>)
    : (body as Record<string, unknown>);
}

function takenByOf(body: unknown): string | null {
  const by = refusalFields(body).acceptedBy;
  if (typeof by === "string" && by) return by;
  const name = (by as { displayName?: unknown } | null)?.displayName;
  return typeof name === "string" && name ? name : null;
}

async function decideRoomTask(roomId: string, taskId: string, verb: "accept" | "dismiss"): Promise<TaskDecision> {
  const result = await postJson<RoomTask | null>(
    roomEndpoint(roomId, `/tasks/${encodeURIComponent(taskId)}/${verb}`),
  );
  if (result.ok) return { ok: true, task: result.value ?? null };
  return { ok: false, status: result.status, message: result.message, takenBy: takenByOf(result.body) };
}

/** `POST /api/rooms/{id}/tasks/{task_id}/accept`. A 409 names who already took it, when the bridge says. */
export function acceptRoomTask(roomId: string, taskId: string): Promise<TaskDecision> {
  return decideRoomTask(roomId, taskId, "accept");
}

/** `POST /api/rooms/{id}/tasks/{task_id}/dismiss`. */
export function dismissRoomTask(roomId: string, taskId: string): Promise<TaskDecision> {
  return decideRoomTask(roomId, taskId, "dismiss");
}

export type RoomOpened = { ok: true; room: Room } | { ok: false; status: number; message: string };

function opened(result: RoomWrite<Room>): RoomOpened {
  if (result.ok && result.value && typeof result.value.id === "string") return { ok: true, room: result.value };
  if (result.ok) return { ok: false, status: 200, message: "The server did not say which workspace it opened." };
  return { ok: false, status: result.status, message: result.message };
}

export interface NewRoom {
  kind: RoomKind;
  title: string;
  agentId?: string;
}

/** `POST /api/rooms` with `{kind, title, agentId}`. */
export async function createRoom(input: NewRoom): Promise<RoomOpened> {
  return opened(await postJson<Room>(ROOMS_ENDPOINT, input));
}

/** `POST /api/rooms/from-task/{task_id}`: the task's room, made or found. */
export async function openTaskRoom(taskId: string): Promise<RoomOpened> {
  return opened(await postJson<Room>(`${ROOMS_ENDPOINT}/from-task/${encodeURIComponent(taskId)}`));
}
