/**
 * The browser's reads and writes on the rooms Bridge API, through the BFF
 * bridge proxy (`/api/bridge/[...path]`), which attaches the caller's own
 * bridge credentials. A refusal or a lost request throws `FetchRefused`, so a
 * hook reports `{ endpoint, status }` instead of showing an empty list.
 */
import { FetchRefused } from "@/lib/query/fetch-refused";
import type { PeriodRange } from "./periods";
import type { Edition, Room, RoomMember, RoomSummary } from "./types";

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

/* ------------------------------------------------------------------------ *
 * Workspace controls (lane 10): definition changes, runs, periods, sharing,
 * revisions, archive, duplicate, reassign, notification choices.
 * ------------------------------------------------------------------------ */

/**
 * A refused mutation, with what the bridge said about it. FastAPI wraps a
 * structured refusal as `{detail: {...}}` and a plain one as `{detail: "..."}`;
 * both are read, as is a flat `{error, message}` body.
 */
export class RoomRequestError extends FetchRefused {
  readonly code: string | null;
  readonly field: string | null;
  readonly currentRev: number | null;
  readonly serverMessage: string | null;

  constructor(endpoint: string, status: number, body: unknown) {
    super(endpoint, status);
    const raw = body && typeof body === "object" ? (body as Record<string, unknown>) : {};
    const detail = raw.detail ?? raw;
    const d: Record<string, unknown> =
      detail && typeof detail === "object" ? (detail as Record<string, unknown>) : {};
    const text = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : null);
    const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
    this.code = text(d.error) ?? text(d.code);
    this.serverMessage = typeof detail === "string" ? text(detail) : (text(d.message) ?? text(d.detail));
    this.currentRev = num(d.currentRev) ?? num(d.current_rev);
    this.field = normaliseField(text(d.field)) ?? guessField(this.code, this.serverMessage);
    if (this.serverMessage) this.message = this.serverMessage;
  }
}

const FIELD_ALIASES: Record<string, string> = {
  schedule: "scheduleText",
  schedule_text: "scheduleText",
  default_grain: "defaultGrain",
  notify_default: "notifyDefault",
};

function normaliseField(field: string | null): string | null {
  return field ? (FIELD_ALIASES[field] ?? field) : null;
}

/** Which form field a refusal is about, when the server named none. */
function guessField(code: string | null, message: string | null): string | null {
  const hay = `${code ?? ""} ${message ?? ""}`.toLowerCase();
  if (hay.includes("schedule") || hay.includes("cron") || hay.includes("interval")) return "scheduleText";
  if (hay.includes("instruction")) return "instructions";
  if (hay.includes("title")) return "title";
  if (hay.includes("time zone") || hay.includes("timezone") || /\btz\b/.test(hay)) return "tz";
  return null;
}

async function send<T>(endpoint: string, method: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(endpoint, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new RoomRequestError(endpoint, 0, null);
  }
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) throw new RoomRequestError(endpoint, res.status, data);
  return data as T;
}

/** The definition fields a form may change (`PATCH /api/rooms/{id}` `changes`). */
export interface DefinitionChanges {
  title?: string;
  instructions?: string;
  scheduleText?: string;
  tz?: string;
  defaultGrain?: "day" | "week" | "month";
  paused?: boolean;
  notifyDefault?: boolean;
}

/** Saves a change against the revision the form was read at; a stale one is a 409. */
export function patchRoom(roomId: string, expectedRev: number, changes: DefinitionChanges): Promise<Room> {
  return send<Room>(roomEndpoint(roomId), "PATCH", { expectedRev, changes });
}

function editionOf(data: unknown): Edition | null {
  if (!data || typeof data !== "object") return null;
  const wrapped = (data as { edition?: unknown }).edition;
  const candidate = (wrapped && typeof wrapped === "object" ? wrapped : data) as Partial<Edition>;
  return typeof candidate.id === "string" && typeof candidate.status === "string" ? (candidate as Edition) : null;
}

/**
 * `POST /api/rooms/{id}/run`. The edition the run is building, when the
 * engine names one (as the edition, or `{edition}`); null when it does not.
 */
export async function runRoom(roomId: string, options: { preview?: boolean } = {}): Promise<Edition | null> {
  const body = options.preview ? { preview: true } : {};
  return editionOf(await send<unknown>(roomEndpoint(roomId, "/run"), "POST", body));
}

/**
 * `POST /api/rooms/{id}/periods` `{grain, anchor}` (a custom range adds its
 * `end`). The stored edition when there is one, else the one being built.
 */
export async function requestPeriod(roomId: string, range: PeriodRange): Promise<Edition> {
  const body: Record<string, string> = { grain: range.grain, anchor: range.start };
  if (range.grain === "custom") body.end = range.end;
  const endpoint = roomEndpoint(roomId, "/periods");
  const edition = editionOf(await send<unknown>(endpoint, "POST", body));
  if (!edition) throw new RoomRequestError(endpoint, 0, null);
  return edition;
}

export interface Revision {
  rev: number;
  changedBy: string | null;
  changedByName?: string | null;
  changedAt: string | null;
  definition: Record<string, unknown>;
}

/** A room's revisions, newest first, in `{revisions: [...]}` or a bare list. */
export async function fetchRevisions(roomId: string, signal?: AbortSignal): Promise<Revision[]> {
  const data = await getJson<unknown>(roomEndpoint(roomId, "/revisions"), signal);
  const rows = Array.isArray(data)
    ? data
    : Array.isArray((data as { revisions?: unknown })?.revisions)
      ? (data as { revisions: unknown[] }).revisions
      : [];
  return (rows as Revision[])
    .filter((r) => r && typeof r.rev === "number")
    .slice()
    .sort((a, b) => b.rev - a.rev);
}

export function restoreRevision(roomId: string, rev: number): Promise<Room> {
  return send<Room>(roomEndpoint(roomId, `/revisions/${encodeURIComponent(String(rev))}/restore`), "POST");
}

export function archiveRoom(roomId: string): Promise<unknown> {
  return send<unknown>(roomEndpoint(roomId, "/archive"), "POST");
}

export function unarchiveRoom(roomId: string): Promise<unknown> {
  return send<unknown>(roomEndpoint(roomId, "/unarchive"), "POST");
}

/** A new workspace assigned to the caller, with the same definition. */
export function duplicateRoom(roomId: string): Promise<Room> {
  return send<Room>(roomEndpoint(roomId, "/duplicate"), "POST");
}

/** Shares with a person, or changes whether they are assigned. */
export function putMember(roomId: string, userId: string, body: { assigned: boolean }): Promise<unknown> {
  return send<unknown>(roomEndpoint(roomId, `/members/${encodeURIComponent(userId)}`), "PUT", body);
}

export function deleteMember(roomId: string, userId: string): Promise<unknown> {
  return send<unknown>(roomEndpoint(roomId, `/members/${encodeURIComponent(userId)}`), "DELETE");
}

/** Admin: make this person assigned and the one the workspace runs as. */
export function reassignRoom(roomId: string, userId: string): Promise<unknown> {
  return send<unknown>(roomEndpoint(roomId, "/reassign"), "POST", { userId });
}

/**
 * One row of the admin list: a summary, with the members and/or the assigned
 * people when the bridge sends them.
 */
export interface AdminRoomRow extends RoomSummary {
  members?: RoomMember[];
  assigned?: Array<{ userId: string; displayName: string }>;
  runsAs?: { userId: string; displayName: string };
}

/** Owner/admin: every workspace in the company (`GET /api/rooms?scope=all`). */
export async function fetchAllRooms(signal?: AbortSignal): Promise<AdminRoomRow[]> {
  const data = await getJson<{ rooms?: unknown }>(`${ROOMS_ENDPOINT}?scope=all`, signal);
  return Array.isArray(data?.rooms) ? (data.rooms as AdminRoomRow[]) : [];
}

export interface Person {
  userId: string;
  displayName: string;
}

export const PEOPLE_ENDPOINT = `${ROOMS_ENDPOINT}/directory`;

/**
 * The company's people, to share with or reassign to: the workspace
 * directory (`GET /api/rooms/directory` -> `{users: [{userId, displayName}]}`),
 * which any member may read. A refusal is thrown, not shown as "nobody".
 */
export async function fetchPeople(signal?: AbortSignal): Promise<Person[]> {
  const data = await getJson<{ users?: unknown }>(PEOPLE_ENDPOINT, signal);
  const rows = Array.isArray(data?.users) ? (data.users as Record<string, unknown>[]) : [];
  return rows
    .filter((u) => u && typeof u.userId === "string" && u.userId)
    .map((u) => {
      const id = u.userId as string;
      const name = typeof u.displayName === "string" && u.displayName.trim() ? u.displayName.trim() : id;
      return { userId: id, displayName: name };
    });
}

export interface LinkedChannel {
  name: string;
  label: string;
}

/** This user's own notification choices for one workspace. */
export interface NotifyPrefs {
  inApp: boolean;
  muted: boolean;
  channels: Record<string, boolean>;
}

export interface NotifyState extends NotifyPrefs {
  /** The external channels this user has linked; only these are offered. */
  linkedChannels: LinkedChannel[];
}

export async function fetchNotify(roomId: string, signal?: AbortSignal): Promise<NotifyState> {
  const data = await getJson<Partial<NotifyState>>(roomEndpoint(roomId, "/notify"), signal);
  const linked = Array.isArray(data?.linkedChannels) ? data.linkedChannels : [];
  return {
    inApp: data?.inApp !== false,
    muted: data?.muted === true,
    channels: data?.channels && typeof data.channels === "object" ? { ...data.channels } : {},
    linkedChannels: linked
      .filter((c): c is LinkedChannel => !!c && typeof c.name === "string" && c.name !== "")
      .map((c) => ({ name: c.name, label: typeof c.label === "string" && c.label ? c.label : c.name })),
  };
}

export function putNotify(roomId: string, prefs: NotifyPrefs): Promise<unknown> {
  return send<unknown>(roomEndpoint(roomId, "/notify"), "PUT", prefs);
}
