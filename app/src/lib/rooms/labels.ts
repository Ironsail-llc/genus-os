import type { RoomKind } from "./types";

/**
 * What a room is called on screen. The only place the word lives: code,
 * routes, types and query params keep `room`, and a rename is one edit here.
 */
export const ROOM_LABEL = {
  /** In a sentence: "This workspace isn't available". */
  singular: "workspace",
  /** As a title: "Workspaces". */
  plural: "Workspaces",
  /** At the start of a sentence: "Workspace". */
  title: "Workspace",
} as const;

/** The rail's groups, in the order they are shown. */
export const ROOM_GROUP_LABELS: Record<RoomKind, string> = {
  report: "Reports",
  agent: "Agents",
  task: "Tasks",
};

/** One room's kind, as its header says it. */
export const ROOM_KIND_LABELS: Record<RoomKind, string> = {
  report: "Report",
  agent: "Agent",
  task: "Task",
};
