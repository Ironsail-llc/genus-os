import type { ReactNode } from "react";
import type { RoomAction } from "@/lib/rooms/access";
import type { Access, Edition, Room } from "@/lib/rooms/types";

/**
 * What a room view hands the controls other lanes plug into it.
 *
 * `can` is the contract's ACTIONS table for this user's access in this room
 * (`lib/rooms/access.ts`): a slot disables what the user may not do (a viewer
 * gets no run, share or settings) rather than hiding it, so the screen says
 * why. The bridge refuses anyway; this only keeps the UI honest.
 */
export interface RoomSlotContext {
  room: Room;
  access: Access;
  can: (action: RoomAction) => boolean;
  /** The edition on show (the latest published one, or the past one picked), or null. */
  edition: Edition | null;
  /** Every published edition, newest first. */
  editions: Edition[];
  /** Show this edition in the pinned place (a period the selector resolved). */
  showEdition: (edition: Edition) => void;
  /** Read the room and its editions again (after a run, a share, a settings change). */
  refresh: () => void;
}

/**
 * The places other lanes fill. Each is optional and renders nothing when
 * absent, so the shell works on its own.
 *
 * Lane 10 (edition pane, period selector, settings, share):
 * - `headerActions`: the header's right-hand controls — settings, share,
 *   run now.
 * - `periodSelector`: beside the pinned edition's title, REPORT rooms only
 *   (agent and task rooms have no periods, and never call it).
 *
 * Lane 11 (hub, inbox, tasks tab, open as thread):
 * - `sections`: below the edition pane and above the conversation — the
 *   room's tasks tab.
 * - The rail's "new in your workspaces" digest is `RoomRail`'s `header` prop.
 * - Thread links are `roomLink(id)` in `lib/rooms/room-id.ts`.
 */
export interface RoomViewSlots {
  headerActions?: (ctx: RoomSlotContext) => ReactNode;
  periodSelector?: (ctx: RoomSlotContext) => ReactNode;
  sections?: (ctx: RoomSlotContext) => ReactNode;
}

/** What the shell passes today. Lanes 10 and 11 fill these in. */
export const DEFAULT_ROOM_SLOTS: RoomViewSlots = {};
