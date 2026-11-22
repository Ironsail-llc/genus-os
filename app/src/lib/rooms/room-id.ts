/**
 * What a room id looks like, and where a room's link goes.
 *
 * The browser names a room by its id and nothing else: never a session key.
 * The shape check is the only judgement the dashboard makes about it; whether
 * the caller may see the room is the engine's and the bridge's to decide.
 */

const ROOM_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isRoomId(value: unknown): value is string {
  return typeof value === "string" && ROOM_ID.test(value);
}

/** The link every room tool result carries. */
export function roomLink(roomId: string): string {
  return `/?v=chat&room=${encodeURIComponent(roomId)}`;
}

/** The main conversation's link. */
export const MAIN_CONVERSATION_LINK = "/?v=chat";
