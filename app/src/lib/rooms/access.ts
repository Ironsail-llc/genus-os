import type { Access } from "./types";

/**
 * What each access level may do in a room — the contract's ACTIONS table,
 * mirrored for the UI only (which controls to enable). The engine and the
 * bridge decide; a control this enables can still be refused there.
 */
export type RoomAction =
  | "read"
  | "post"
  | "run"
  | "request_period"
  | "accept_task"
  | "edit_definition"
  | "share"
  | "archive"
  | "restore_revision"
  | "duplicate"
  | "reassign"
  | "list_all";

const VIEWER: RoomAction[] = ["read"];
const SHARED: RoomAction[] = ["read", "post", "run", "request_period", "accept_task", "duplicate"];
const ASSIGNED: RoomAction[] = [...SHARED, "edit_definition", "share", "archive", "restore_revision"];
const ADMIN: RoomAction[] = [...ASSIGNED, "reassign", "list_all"];

const ALLOWED: Record<Access, ReadonlySet<RoomAction>> = {
  viewer: new Set(VIEWER),
  shared: new Set(SHARED),
  assigned: new Set(ASSIGNED),
  admin: new Set(ADMIN),
};

export function can(access: Access, action: RoomAction): boolean {
  return Object.hasOwn(ALLOWED, access) ? ALLOWED[access].has(action) : false;
}
