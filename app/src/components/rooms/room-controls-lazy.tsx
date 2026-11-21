"use client";

import dynamic from "next/dynamic";
import { useSession } from "next-auth/react";
import type { RoomViewSlots } from "@/components/rooms/room-slots";

/*
 * The shell imports this, not the controls themselves: the header actions,
 * the period selector and their drawers are fetched with the first workspace
 * opened, and the admin list only for an owner or admin, so none of it adds
 * to the dashboard's first load.
 */

const RoomHeaderActions = dynamic(() => import("@/components/rooms/room-controls").then((m) => m.RoomHeaderActions), {
  ssr: false,
});
const PeriodSelector = dynamic(() => import("@/components/rooms/period-selector").then((m) => m.PeriodSelector), {
  ssr: false,
});
const AllRoomsAdminEntry = dynamic(() => import("@/components/rooms/all-rooms-admin").then((m) => m.AllRoomsAdminEntry), {
  ssr: false,
});

/** The shell's room slots with lane 10's controls in place (`app-shell.tsx` passes this). */
export const ROOM_SLOTS_WITH_CONTROLS: RoomViewSlots = {
  headerActions: (ctx) => <RoomHeaderActions ctx={ctx} />,
  periodSelector: (ctx) => <PeriodSelector ctx={ctx} />,
};

const ADMIN_ROLES = new Set(["owner", "admin"]);

/** The rail's "All company workspaces" entry: loaded only for an owner or admin. */
export function RailAdminEntry({ onOpenRoom }: { onOpenRoom: (roomId: string) => void }) {
  const { data: session } = useSession();
  if (!ADMIN_ROLES.has(String(session?.role ?? ""))) return null;
  return <AllRoomsAdminEntry onOpenRoom={onOpenRoom} />;
}
