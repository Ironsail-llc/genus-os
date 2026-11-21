/**
 * The hub's view of rooms: which ones have something new, and a room's tasks.
 *
 * `RoomTask` is the shape this lane reads from `GET /api/rooms/{id}/tasks`
 * (and gets back from accept/dismiss). The contract names the routes, not the
 * row; this is the row, camelCase like the rest of the bridge:
 *
 *   { taskId, title, status, origin, editionId, assignee, acceptedBy, dismissed }
 *
 * `status` is the CRM task's own status. `origin` and `editionId` are the
 * `room_tasks` link (`editionId` null unless the task came from an edition's
 * suggestion). `assignee` is who the CRM task is assigned to; `acceptedBy` who
 * took it in the room (null until someone does). Both name a person, never
 * only an id, so the tab can say who.
 */
import type { RoomSummary } from "./types";

export type RoomTaskStatus = "TODO" | "IN_PROGRESS" | "REVIEW" | "DONE";
export type RoomTaskOrigin = "subject" | "suggestion" | "linked";

export interface RoomPerson {
  userId: string;
  displayName: string;
}

export interface RoomTask {
  taskId: string;
  title: string;
  status: RoomTaskStatus;
  origin: RoomTaskOrigin;
  editionId: string | null;
  assignee: RoomPerson | null;
  acceptedBy: RoomPerson | null;
  dismissed: boolean;
}

export const TASK_STATUS_LABELS: Record<RoomTaskStatus, string> = {
  TODO: "To do",
  IN_PROGRESS: "In progress",
  REVIEW: "In review",
  DONE: "Done",
};

function activityTime(room: RoomSummary): number {
  const t = Date.parse(room.lastActivityAt ?? "");
  return Number.isFinite(t) ? t : 0;
}

/**
 * The rooms with unread activity, newest activity first: what "New in your
 * workspaces" lists. Archived rooms and the room that is open are left out.
 */
export function unreadRooms(rooms: RoomSummary[], activeRoomId: string | null = null): RoomSummary[] {
  return rooms
    .filter((room) => !room.archived && room.id !== activeRoomId && (room.unread || 0) > 0)
    .sort((a, b) => activityTime(b) - activityTime(a));
}

/**
 * A room's tasks as the tab shows them: open suggestions (nobody has taken
 * them) and taken work (accepted suggestions, the room's own subject task and
 * linked tasks). Dismissed rows are gone from both; a task is decided once.
 */
export function splitRoomTasks(tasks: RoomTask[]): { suggested: RoomTask[]; taken: RoomTask[] } {
  const live = tasks.filter((task) => !task.dismissed);
  return {
    suggested: live.filter((task) => task.origin === "suggestion" && !task.acceptedBy),
    taken: live.filter((task) => task.origin !== "suggestion" || Boolean(task.acceptedBy)),
  };
}
