/**
 * Server-side: proof that the engine answered for the ROOM, not for the
 * caller's main conversation.
 *
 * A room request carries no session key, and an engine that ignores
 * `room_id` reads "no key" as the caller's main conversation. So every room
 * answer must echo the room (`roomId`): a JSON field on history, abort, steer,
 * interrupt and outcome replies, and, for a streamed send, the first SSE event
 * (an event named `room` carrying `{roomId}`, or any first event whose data
 * has `roomId`). No echo, or another room's, and the route refuses (502) and
 * hands nothing on.
 *
 * Before anything is WRITTEN (a send, a steer), the room is confirmed with a
 * one-row history read, so an engine that would misfile the message never
 * receives it. A confirmation is kept per caller and room for a short while.
 */
import { createHash } from "node:crypto";

import { bridgeAuthHeaders } from "@/lib/bridge-auth";

export const ROOM_NOT_CONFIRMED =
  "the engine did not confirm this workspace, so nothing was sent; it may not support workspaces yet";
export const ROOM_ANSWER_NOT_CONFIRMED =
  "the engine's answer did not confirm this workspace, so it is not shown";

/** The room an engine answer names, or null. */
export function echoedRoom(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const { roomId, room_id } = payload as { roomId?: unknown; room_id?: unknown };
  const id = typeof roomId === "string" ? roomId : typeof room_id === "string" ? room_id : null;
  return id || null;
}

export function echoesRoom(payload: unknown, roomId: string): boolean {
  return echoedRoom(payload) === roomId;
}

const CONFIRM_TTL_MS = 60_000;
const confirmed = new Map<string, number>();

async function callerRoomKey(roomId: string): Promise<string> {
  const headers = await bridgeAuthHeaders();
  const canonical = Object.entries(headers)
    .map(([name, value]) => [name.toLowerCase(), value])
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `${createHash("sha256").update(JSON.stringify(canonical)).digest("hex")}:${roomId}`;
}

/**
 * Whether the engine serves `roomId` for this caller, by `readHistory`'s echo.
 * Only a confirmation is remembered; a refusal or a failure is asked again.
 */
export async function confirmRoom(roomId: string, readHistory: () => Promise<unknown>): Promise<boolean> {
  const key = await callerRoomKey(roomId);
  const now = Date.now();
  const until = confirmed.get(key);
  if (until !== undefined && until > now) return true;
  let answer: unknown;
  try {
    answer = await readHistory();
  } catch {
    return false;
  }
  if (!echoesRoom(answer, roomId)) return false;
  for (const [k, expires] of confirmed) if (expires <= now) confirmed.delete(k);
  confirmed.set(key, now + CONFIRM_TTL_MS);
  return true;
}

const MAX_PEEK_BYTES = 64 * 1024;

function firstEvent(text: string): { event: string; data: unknown } | null {
  const end = text.search(/\r?\n\r?\n/);
  if (end < 0) return null;
  let event = "message";
  const data: string[] = [];
  for (const line of text.slice(0, end).split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  try {
    return { event, data: JSON.parse(data.join("\n")) };
  } catch {
    return { event, data: null };
  }
}

/**
 * The send stream, when its first event confirms `roomId`; null otherwise
 * (the engine's stream is cancelled and nothing of it is forwarded). The
 * bytes read to decide are forwarded unchanged ahead of the rest.
 */
export async function confirmRoomStream(body: ReadableStream<Uint8Array>, roomId: string): Promise<ReadableStream<Uint8Array> | null> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const head: Uint8Array[] = [];
  let text = "";
  let size = 0;
  let verdict: boolean | null = null;
  while (verdict === null) {
    const { value, done } = await reader.read();
    if (value) {
      head.push(value);
      size += value.byteLength;
      text += decoder.decode(value, { stream: true });
    }
    const first = firstEvent(text);
    if (first) verdict = (first.event === "room" || first.data !== null) && echoesRoom(first.data, roomId);
    else if (done || size > MAX_PEEK_BYTES) verdict = false;
  }
  if (!verdict) {
    await reader.cancel().catch(() => {});
    return null;
  }
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of head) controller.enqueue(chunk);
    },
    async pull(controller) {
      const { value, done } = await reader.read();
      if (done) controller.close();
      else controller.enqueue(value);
    },
    cancel(reason) {
      return reader.cancel(reason);
    },
  });
}
