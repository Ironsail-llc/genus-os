/**
 * Room events on the `agent` stream (contract → Events): `room.edition`,
 * `room.message`, `room.updated`, `room.task`, each with a payload of
 * `{room_id, tenant_id, ...}`.
 */

export const ROOM_EVENT_PREFIX = "room.";

export function isRoomEventType(type: unknown): boolean {
  return typeof type === "string" && type.startsWith(ROOM_EVENT_PREFIX);
}

/** The room an event is about, or null when it names none. */
export function roomIdOf(event: { payload?: Record<string, unknown> | null }): string | null {
  const id = event.payload?.room_id;
  return typeof id === "string" && id ? id : null;
}
