/**
 * Server-side: which conversation a chat request is for.
 *
 * Either a ROOM, named by id, or an AGENT (or neither: the main session). The
 * room is never turned into a session key here. The route checks only that
 * the value has a room id's shape and forwards it to the engine as `room_id`
 * with the caller's own token; the engine resolves the key and decides whether
 * this caller may read or post there (a room they cannot see is a 404).
 *
 * With no room named this is `resolveChatAgent`, unchanged — the guard that
 * keeps the operator's shared main session shared.
 */
import { resolveChatAgent } from "@/lib/chat/agent-guard";
import { isRoomId } from "@/lib/rooms/room-id";

export type ChatTarget =
  | { ok: true; key: string; roomId?: undefined }
  | { ok: true; key: ""; roomId: string }
  | { ok: false; status: 400 | 403; error: string };

const NOT_A_ROOM = "room must be a room id";
const ROOM_OR_AGENT = "name a room or an agent, not both";

function named(value: unknown): boolean {
  return value !== undefined && value !== null && value !== "";
}

export async function resolveChatTarget(agent: unknown, room: unknown): Promise<ChatTarget> {
  if (!named(room)) {
    const chosen = await resolveChatAgent(agent);
    return chosen.ok ? { ok: true, key: chosen.key } : chosen;
  }
  if (!isRoomId(room)) return { ok: false, status: 400, error: NOT_A_ROOM };
  // A room's agent is the room's; a request naming another as well is not one
  // this route can honour without guessing which the caller meant.
  if (named(agent)) return { ok: false, status: 400, error: ROOM_OR_AGENT };
  return { ok: true, key: "", roomId: room };
}
