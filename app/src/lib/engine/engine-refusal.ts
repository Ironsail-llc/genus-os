/**
 * The engine refused the request itself (400, 413 or 422), or failed before it
 * admitted a turn and said so (`request_admitted: false`): its status and its
 * words, as opposed to an engine that failed mid-turn. The chat send route answers with
 * both and `request_admitted: false`, so the browser shows the reason instead
 * of a recovery card for a turn that never started.
 *
 * Its own module so a route test that mocks the engine client can still raise
 * the real class.
 */
export class EngineRefusal extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = "EngineRefusal";
  }
}

/** The engine statuses that mean "not this request", not "the engine broke". */
export const REFUSAL_STATUSES = new Set([400, 413, 422]);

/**
 * A send to a room whose agent is already answering somebody: the engine's
 * 409 `{error: "room_busy", answering}`. Nothing was admitted; the browser
 * queues the message and says whom the agent is answering.
 */
export class RoomBusy extends Error {
  constructor(public readonly answering: string) {
    super("room_busy");
    this.name = "RoomBusy";
  }
}
