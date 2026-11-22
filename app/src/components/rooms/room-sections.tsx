"use client";

import dynamic from "next/dynamic";
import type { RoomSlotContext, RoomViewSlots } from "./room-slots";

// The tasks tab rides with the room view's own chunk, never the chat home's:
// it is fetched the first time a workspace with tasks opens.
const RoomTasksTab = dynamic(() => import("./room-tasks-tab").then((m) => m.RoomTasksTab), { ssr: false });

/**
 * Lane 11's `sections` slot: the workspace's tasks tab, under the edition
 * pane. Report rooms carry their editions' suggestions and task rooms their
 * subject task; an agent room has no tasks of its own, so it keeps the whole
 * width for the conversation.
 */
export function roomSections(ctx: RoomSlotContext) {
  if (ctx.room.kind === "agent") return null;
  return <RoomTasksTab key={ctx.room.id} ctx={ctx} />;
}

export const HUB_ROOM_SLOTS: RoomViewSlots = { sections: roomSections };
