"use client";

import { useState, type ReactNode } from "react";
import { BarChart3, Bot, ChevronDown, ListChecks, MessageSquare, PauseCircle } from "lucide-react";
import { RailAdminEntry } from "@/components/rooms/room-controls-lazy";
import { ROOM_GROUP_LABELS, ROOM_LABEL } from "@/lib/rooms/labels";
import type { RoomKind, RoomSummary } from "@/lib/rooms/types";

const GROUP_ORDER: RoomKind[] = ["report", "agent", "task"];
const KIND_ICONS = { report: BarChart3, agent: Bot, task: ListChecks } as const;

export interface RoomRailProps {
  rooms: RoomSummary[];
  /** The open room, or null for the main conversation. */
  activeRoomId: string | null;
  /** A room id, or null for the main conversation. */
  onSelect: (roomId: string | null) => void;
  isLoading?: boolean;
  error?: { endpoint: string; status: number } | null;
  /** Narrow screens: one button that opens the list over the chat. */
  collapsed?: boolean;
  /**
   * Lane 11 slot: drawn under "Main conversation", above the groups (the
   * "new in your workspaces" digest). Nothing by default.
   */
  header?: ReactNode;
}

/**
 * The caller's workspaces, beside the chat: the main conversation first, then
 * the rooms grouped Reports / Agents / Tasks, then anything archived. Styled
 * as the sidebar's own nav so it reads as part of the shell.
 */
export function RoomRail(props: RoomRailProps) {
  const { collapsed = false } = props;
  const [open, setOpen] = useState(false);

  if (!collapsed) {
    return (
      <aside
        className="flex w-60 shrink-0 flex-col border-r border-border bg-sidebar/40 px-2.5 py-3"
        data-testid="room-rail"
      >
        <RailNav {...props} />
      </aside>
    );
  }

  const active = props.activeRoomId ? props.rooms.find((r) => r.id === props.activeRoomId) : undefined;
  const unread = totalUnread(props.rooms, props.activeRoomId);
  return (
    <div className="relative shrink-0 border-b border-border bg-background" data-testid="room-rail">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-2 text-left text-xs text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        data-testid="room-rail-toggle"
      >
        <span className="font-medium uppercase tracking-[0.09em] text-[10.5px]">{ROOM_LABEL.plural}</span>
        <span className="truncate text-foreground">{active ? active.title : "Main conversation"}</span>
        {unread > 0 && <UnreadBadge count={unread} />}
        <ChevronDown className={`ml-auto size-3.5 shrink-0 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="absolute inset-x-0 top-full z-30 max-h-[70vh] overflow-y-auto border-b border-border bg-background px-2.5 py-2 shadow-lg">
          <RailNav
            {...props}
            onSelect={(roomId) => {
              setOpen(false);
              props.onSelect(roomId);
            }}
          />
        </div>
      )}
    </div>
  );
}

function totalUnread(rooms: RoomSummary[], activeRoomId: string | null): number {
  return rooms.reduce(
    (sum, room) => sum + (room.archived || room.id === activeRoomId ? 0 : Math.max(0, room.unread || 0)),
    0,
  );
}

function RailNav({ rooms, activeRoomId, onSelect, isLoading, error, header }: RoomRailProps) {
  const live = rooms.filter((room) => !room.archived);
  const archived = rooms.filter((room) => room.archived);
  const groups = GROUP_ORDER.map((kind) => ({ kind, rooms: live.filter((room) => room.kind === kind) })).filter(
    (group) => group.rooms.length > 0,
  );

  return (
    <nav aria-label={ROOM_LABEL.plural} className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto">
      <RailButton
        active={activeRoomId === null}
        onClick={() => onSelect(null)}
        testId="room-rail-main"
        icon={<MessageSquare className="size-4" />}
        label="Main conversation"
      />
      {header}
      {error && (
        <p className="px-2 pt-3 text-xs text-muted-foreground" data-testid="room-rail-error">
          {ROOM_LABEL.plural} unavailable ({error.status})
        </p>
      )}
      {!error && !isLoading && rooms.length === 0 && (
        <p className="px-2 pt-3 text-xs leading-relaxed text-muted-foreground" data-testid="room-rail-empty">
          No {ROOM_LABEL.plural.toLowerCase()} yet. Ask the agent to set up a report, or to open a task or a
          conversation with another agent as a {ROOM_LABEL.singular}.
        </p>
      )}
      {!error && isLoading && rooms.length === 0 && (
        <div className="flex flex-col gap-1.5 px-2 pt-3" aria-busy="true" aria-label={`Loading ${ROOM_LABEL.plural.toLowerCase()}`}>
          <div className="h-4 w-32 animate-pulse rounded bg-muted" />
          <div className="h-4 w-40 animate-pulse rounded bg-muted/70" />
        </div>
      )}
      {groups.map((group) => (
        <RailGroup key={group.kind} id={group.kind} label={ROOM_GROUP_LABELS[group.kind]}>
          {group.rooms.map((room) => (
            <RoomButton key={room.id} room={room} active={room.id === activeRoomId} onSelect={onSelect} />
          ))}
        </RailGroup>
      ))}
      {archived.length > 0 && (
        <RailGroup id="archived" label="Archived">
          {archived.map((room) => (
            <RoomButton key={room.id} room={room} active={room.id === activeRoomId} onSelect={onSelect} />
          ))}
        </RailGroup>
      )}
      <RailAdminEntry onOpenRoom={onSelect} />
    </nav>
  );
}

function RailGroup({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-0.5" data-testid={`room-group-${id}`} aria-labelledby={`room-group-${id}-label`}>
      <h3
        id={`room-group-${id}-label`}
        className="px-2 pt-3 pb-1 text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted-foreground/70"
      >
        {label}
      </h3>
      {children}
    </section>
  );
}

function RoomButton({
  room,
  active,
  onSelect,
}: {
  room: RoomSummary;
  active: boolean;
  onSelect: (roomId: string) => void;
}) {
  const Icon = KIND_ICONS[room.kind] ?? MessageSquare;
  const unread = active || room.archived ? 0 : Math.max(0, room.unread || 0);
  return (
    <RailButton
      active={active}
      onClick={() => onSelect(room.id)}
      testId={`room-rail-item-${room.id}`}
      icon={<Icon className="size-4" />}
      label={room.title}
      muted={room.archived}
      trailing={
        <>
          {room.paused && !room.archived && (
            <span className="ml-auto inline-flex items-center gap-0.5 text-[10px] text-muted-foreground" title="Scheduled runs are paused">
              <PauseCircle className="size-3" aria-hidden />
              Paused
            </span>
          )}
          {unread > 0 && <UnreadBadge count={unread} testId={`room-unread-${room.id}`} />}
        </>
      }
    />
  );
}

function RailButton({
  active,
  onClick,
  testId,
  icon,
  label,
  trailing,
  muted = false,
}: {
  active: boolean;
  onClick: () => void;
  testId: string;
  icon: ReactNode;
  label: string;
  trailing?: ReactNode;
  muted?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={`relative flex w-full items-center gap-2.5 rounded-md border px-2 py-1.5 text-left text-[13px] transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-ring ${
        active
          ? "border-primary/25 bg-primary/10 text-foreground"
          : `border-transparent ${muted ? "text-muted-foreground/70" : "text-foreground/70"} hover:bg-muted/60 hover:text-foreground`
      }`}
      data-testid={testId}
    >
      <span className={`shrink-0 ${active ? "text-primary" : ""}`}>{icon}</span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {trailing}
    </button>
  );
}

function UnreadBadge({ count, testId }: { count: number; testId?: string }) {
  return (
    <span
      className="ml-auto flex h-4 min-w-[16px] shrink-0 items-center justify-center rounded-full bg-primary px-1 font-mono text-[10px] font-medium text-primary-foreground"
      data-testid={testId}
      aria-label={`${count} unread`}
    >
      {count > 99 ? "99+" : count}
    </span>
  );
}
