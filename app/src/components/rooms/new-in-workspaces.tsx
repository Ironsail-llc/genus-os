"use client";

import { useState } from "react";
import { BarChart3, Bot, ChevronDown, ListChecks, MessageSquare } from "lucide-react";
import { useRooms } from "@/hooks/use-rooms";
import { unreadRooms } from "@/lib/rooms/hub";
import { formatPeriod } from "@/lib/rooms/format";
import { ROOM_LABEL } from "@/lib/rooms/labels";
import { navigateTo } from "@/lib/rooms/navigate";
import { roomLink } from "@/lib/rooms/room-id";
import type { RoomSummary } from "@/lib/rooms/types";
import { NewWorkspaceButton } from "./open-as-workspace";

const KIND_ICONS = { report: BarChart3, agent: Bot, task: ListChecks } as const;
const HEADING = `New in your ${ROOM_LABEL.plural.toLowerCase()}`;

export interface NewInWorkspacesProps {
  rooms: RoomSummary[];
  /** Left out of the list: the room that is open has nothing new to say. */
  activeRoomId?: string | null;
  onOpen: (roomId: string) => void;
  isLoading?: boolean;
  error?: { endpoint: string; status: number } | null;
  /** "landing" on the chat home; "rail" at the top of the workspaces rail. */
  variant?: "landing" | "rail";
}

/**
 * The workspaces with unread activity (new messages, a new edition), newest
 * first. Says plainly when nothing is new; says nothing while the list is
 * loading, and names the failure when it could not be read, so an empty list
 * is never a guess.
 */
export function NewInWorkspaces({
  rooms,
  activeRoomId = null,
  onOpen,
  isLoading = false,
  error = null,
  variant = "landing",
}: NewInWorkspacesProps) {
  const fresh = unreadRooms(rooms, activeRoomId);
  const rail = variant === "rail";
  const headingId = `new-in-workspaces-${variant}-label`;
  // On the rail the same rooms already carry their badges in their groups, so
  // the digest folds to one line until asked; the landing lists them outright.
  const [expanded, setExpanded] = useState(false);
  const showList = !rail || expanded;

  return (
    <section
      aria-labelledby={headingId}
      data-testid="new-in-workspaces"
      className={rail ? "flex flex-col gap-0.5" : "flex flex-col gap-2"}
    >
      <h3
        id={headingId}
        className={
          rail
            ? "px-2 pt-3 pb-1 text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted-foreground/70"
            : "text-[11px] font-medium uppercase tracking-[0.07em] text-muted-foreground/80"
        }
      >
        {HEADING}
      </h3>
      {error ? (
        <p className={`text-xs text-muted-foreground ${rail ? "px-2" : ""}`} data-testid="new-in-workspaces-error">
          {ROOM_LABEL.plural} unavailable ({error.status})
        </p>
      ) : isLoading && rooms.length === 0 ? null : fresh.length === 0 ? (
        <p
          className={`text-xs text-muted-foreground ${rail ? "px-2" : ""}`}
          data-testid="new-in-workspaces-empty"
        >
          Nothing new since you last looked.
        </p>
      ) : (
        <>
          {rail && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              aria-controls="new-in-workspaces-rail-list"
              className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-xs text-foreground/80 transition-colors hover:bg-muted/60 hover:text-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              data-testid="new-in-workspaces-toggle"
            >
              <span className="size-1.5 shrink-0 rounded-full bg-primary" aria-hidden />
              <span className="flex-1 truncate">
                {fresh.length === 1
                  ? `1 ${ROOM_LABEL.singular} updated`
                  : `${fresh.length} ${ROOM_LABEL.plural.toLowerCase()} updated`}
              </span>
              <ChevronDown className={`size-3.5 shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`} aria-hidden />
            </button>
          )}
          {showList && (
            <ul
              id={rail ? "new-in-workspaces-rail-list" : undefined}
              className={rail ? "flex flex-col gap-0.5" : "glass-panel divide-y divide-border overflow-hidden p-0"}
            >
              {fresh.map((room) => (
                <li key={room.id}>
                  <FreshRoom room={room} rail={rail} onOpen={onOpen} />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function FreshRoom({ room, rail, onOpen }: { room: RoomSummary; rail: boolean; onOpen: (id: string) => void }) {
  const Icon = KIND_ICONS[room.kind] ?? MessageSquare;
  const edition = room.latestEdition;
  const period = edition ? formatPeriod(edition.periodStart, edition.periodEnd) : "";
  const count = room.unread > 99 ? "99+" : String(room.unread);
  return (
    <button
      type="button"
      onClick={() => onOpen(room.id)}
      data-testid={`new-in-workspaces-item-${room.id}`}
      className={
        rail
          ? "flex w-full items-center gap-2.5 rounded-md border border-transparent px-2 py-1.5 text-left text-[13px] text-foreground/80 transition-colors hover:bg-muted/60 hover:text-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          : "flex min-h-[44px] w-full items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-accent/60 focus:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring"
      }
    >
      <Icon className={`size-4 shrink-0 ${rail ? "" : "text-muted-foreground"}`} aria-hidden />
      <span className="min-w-0 flex-1">
        <span className={`block truncate ${rail ? "" : "text-sm font-medium text-foreground"}`}>{room.title}</span>
        {!rail && edition && (
          <span className="block truncate text-xs text-muted-foreground">
            {edition.title}
            {period ? ` · ${period}` : ""}
          </span>
        )}
      </span>
      <span
        className="ml-auto flex h-4 min-w-[16px] shrink-0 items-center justify-center rounded-full bg-primary px-1 font-mono text-[10px] font-medium text-primary-foreground"
        aria-label={`${room.unread} unread`}
      >
        {count}
      </span>
    </button>
  );
}

/** The chat landing's copy, reading the shared rooms cache and opening in place. */
export function LandingNewInWorkspaces() {
  const { rooms, isLoading, error, available } = useRooms();
  // Where nothing serves workspaces (the list answers 404) the landing says
  // nothing about them, as the shell shows no rail.
  if (!available) return null;
  return (
    <NewInWorkspaces
      rooms={rooms}
      isLoading={isLoading}
      error={error}
      onOpen={(roomId) => navigateTo(roomLink(roomId))}
    />
  );
}

export interface RoomRailHeaderProps {
  rooms: RoomSummary[];
  activeRoomId: string | null;
  onSelect: (roomId: string) => void;
  isLoading?: boolean;
  error?: { endpoint: string; status: number } | null;
}

/** The rail's `header` slot: what is new, then "New workspace". */
export function RoomRailHeader({ rooms, activeRoomId, onSelect, isLoading, error }: RoomRailHeaderProps) {
  return (
    <div className="flex flex-col gap-1" data-testid="room-rail-header">
      <div className="px-0.5 pt-1">
        <NewWorkspaceButton variant="rail" />
      </div>
      {/* The rail draws its own error; the digest stays quiet rather than say it twice. */}
      {!error && (
        <NewInWorkspaces
          rooms={rooms}
          activeRoomId={activeRoomId}
          onOpen={onSelect}
          isLoading={isLoading}
          variant="rail"
        />
      )}
    </div>
  );
}
