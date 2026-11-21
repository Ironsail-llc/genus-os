"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { BarChart3, Bot, ListChecks, PauseCircle, Archive, UserRound } from "lucide-react";
import { ChatPanel } from "@/components/chat-panel";
import { EditionCard } from "@/components/rooms/edition-card";
import { DEFAULT_ROOM_SLOTS, type RoomSlotContext, type RoomViewSlots } from "@/components/rooms/room-slots";
import { useRoomEvents } from "@/hooks/use-rooms";
import { can, type RoomAction } from "@/lib/rooms/access";
import { fetchEditions, fetchRoom, postRoomRead } from "@/lib/rooms/api";
import { formatPeriod, formatWhen } from "@/lib/rooms/format";
import { ROOM_KIND_LABELS, ROOM_LABEL } from "@/lib/rooms/labels";
import { MAIN_CONVERSATION_LINK } from "@/lib/rooms/room-id";
import type { Access, Edition, Room } from "@/lib/rooms/types";
import { FetchRefused } from "@/lib/query/fetch-refused";
import { useAppQueryClient } from "@/lib/query/query-client";

const KIND_ICONS = { report: BarChart3, agent: Bot, task: ListChecks } as const;

export interface RoomViewProps {
  roomId: string;
  /** Back to the main conversation. */
  onLeave: () => void;
  /**
   * Record that this user has read the room. The shell passes `useRooms`'
   * `markRead`, which also clears the rail's badge; on its own the view posts
   * `POST /api/rooms/{id}/read` itself.
   */
  markRead?: (roomId: string) => Promise<void> | void;
  /** Places other lanes fill (`room-slots.ts`). */
  slots?: RoomViewSlots;
}

/** Shown, pinned or listed: a published edition that is not somebody's preview. */
function isPublished(edition: Edition): boolean {
  return edition.status === "published" && edition.preview !== true;
}

function isUnavailable(error: unknown): boolean {
  return error instanceof FetchRefused && (error.status === 404 || error.status === 403);
}

/**
 * One workspace: its header, then (for a report, or any room that has
 * editions) the latest published edition pinned as its dashboard with the
 * past editions under it, and the conversation — the ordinary chat panel,
 * pointed at the room.
 *
 * `room.*` events for this room, and only this room, bring a new edition or
 * message in without a refresh; other tenants' events never reach the browser
 * (`lib/event-bus/tenant-filter.ts`).
 */
export function RoomView({ roomId, onLeave, markRead, slots = DEFAULT_ROOM_SLOTS }: RoomViewProps) {
  const client = useAppQueryClient();
  const roomKey = useMemo(() => ["rooms", roomId, "detail"] as const, [roomId]);
  const editionsKey = useMemo(() => ["rooms", roomId, "editions"] as const, [roomId]);

  const roomQuery = useQuery(
    { queryKey: roomKey, queryFn: ({ signal }) => fetchRoom(roomId, signal), staleTime: 15_000 },
    client,
  );
  const room = roomQuery.data;
  const editionsQuery = useQuery(
    {
      queryKey: editionsKey,
      queryFn: ({ signal }) => fetchEditions(roomId, signal),
      enabled: Boolean(room),
      staleTime: 15_000,
    },
    client,
  );

  const [pickedId, setPickedId] = useState<string | null>(null);
  const [chatRefresh, setChatRefresh] = useState(0);

  const read = useCallback(() => {
    if (markRead) void markRead(roomId);
    else void postRoomRead(roomId);
  }, [markRead, roomId]);

  // Opening the room reads it, once per room, and only a room that answered.
  const readFor = useRef<string | null>(null);
  useEffect(() => {
    if (!room || readFor.current === roomId) return;
    readFor.current = roomId;
    read();
  }, [room, roomId, read]);

  useRoomEvents(roomId, (event) => {
    if (event.type === "room.edition") {
      void client.invalidateQueries({ queryKey: editionsKey });
      setChatRefresh((n) => n + 1);
      read();
    } else if (event.type === "room.message") {
      setChatRefresh((n) => n + 1);
      read();
    } else if (event.type === "room.updated" || event.type === "room.task") {
      void client.invalidateQueries({ queryKey: roomKey });
    }
  });

  const refresh = useCallback(() => {
    void client.invalidateQueries({ queryKey: roomKey });
    void client.invalidateQueries({ queryKey: editionsKey });
  }, [client, roomKey, editionsKey]);

  const published = useMemo(() => (editionsQuery.data ?? []).filter(isPublished), [editionsQuery.data]);
  const latest = published[0] ?? null;
  const picked = pickedId ? (published.find((e) => e.id === pickedId) ?? null) : null;
  const shown = picked ?? latest;

  if (roomQuery.isError && isUnavailable(roomQuery.error)) {
    return <RoomUnavailable onLeave={onLeave} />;
  }
  if (roomQuery.isError) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center" data-testid="room-error">
        <p className="text-sm font-medium text-foreground">This {ROOM_LABEL.singular} could not be loaded</p>
        <p className="max-w-sm text-xs text-muted-foreground">
          {roomQuery.error instanceof FetchRefused
            ? `The server answered ${roomQuery.error.status || "nothing"}. Nothing has been changed.`
            : "Nothing has been changed."}
        </p>
        <button
          type="button"
          onClick={() => void roomQuery.refetch()}
          className="mt-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-accent"
        >
          Retry
        </button>
      </div>
    );
  }
  if (!room) {
    return (
      <div className="flex h-full flex-col gap-3 p-4" data-testid="room-loading" aria-busy="true" aria-label="Loading">
        <div className="h-6 w-56 animate-pulse rounded-md bg-muted" />
        <div className="h-40 w-full max-w-3xl animate-pulse rounded-lg bg-muted/60" />
      </div>
    );
  }

  // A missing access level is read as the least one: the composer stays shut
  // rather than offering a post the engine would refuse.
  const access: Access = room.access ?? "viewer";
  const archived = room.archived === true;
  const canPost = can(access, "post", archived);
  const ctx: RoomSlotContext = {
    room,
    access,
    // The room's archived flag is bound here: `ctx.can(action)` keeps its shape.
    can: (action: RoomAction) => can(access, action, archived),
    edition: shown,
    editions: published,
    showEdition: (edition) => setPickedId(edition.id === latest?.id ? null : edition.id),
    refresh,
  };
  const isReport = room.kind === "report";
  const sections = slots.sections?.(ctx) ?? null;
  const showPane = isReport || published.length > 0 || sections !== null;
  const past = published.filter((e) => e.id !== shown?.id);

  return (
    <section className="flex h-full min-h-0 w-full flex-col bg-background" data-testid="room-view" aria-label={room.title}>
      <RoomHeader room={room} actions={slots.headerActions?.(ctx)} />
      <div className={`flex min-h-0 flex-1 flex-col ${showPane ? "xl:flex-row" : ""}`}>
        {showPane && (
          <div
            className="max-h-[45vh] shrink-0 overflow-y-auto border-b border-border xl:max-h-none xl:flex-1 xl:shrink xl:border-b-0 xl:border-r"
            data-testid="room-editions"
          >
            <div className="mx-auto flex w-full max-w-4xl flex-col gap-5 p-4">
              {(isReport && slots.periodSelector) && (
                <div className="flex items-center justify-end gap-2" data-slot="period-selector">
                  {slots.periodSelector(ctx)}
                </div>
              )}
              {shown ? (
                <div
                  className="rounded-xl border border-border bg-card p-4 shadow-sm"
                  data-testid="room-pinned-edition"
                >
                  <EditionCard
                    edition={shown}
                    actions={
                      picked ? (
                        <button
                          type="button"
                          onClick={() => setPickedId(null)}
                          className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                          data-testid="room-edition-latest"
                        >
                          Back to latest
                        </button>
                      ) : (
                        <span className="rounded-full border border-primary/25 bg-primary/10 px-2 py-0.5 text-[10.5px] font-medium text-primary">
                          Latest
                        </span>
                      )
                    }
                  />
                </div>
              ) : (
                isReport && !editionsQuery.isPending && <NoEdition room={room} />
              )}
              {past.length > 0 && <PastEditions editions={past} onPick={(e) => ctx.showEdition(e)} />}
              {sections}
            </div>
          </div>
        )}
        <div className={`min-h-0 flex-1 ${showPane ? "xl:w-[440px] xl:flex-none" : ""}`}>
          <div className={`h-full ${showPane ? "" : "mx-auto w-full max-w-5xl"}`}>
            <ChatPanel
              variant="dock"
              room={{
                id: room.id,
                readOnly: !canPost,
                ...(!canPost ? { readOnlyReason: archived ? ("archived" as const) : ("viewer" as const) } : {}),
                refreshKey: chatRefresh,
              }}
            />
          </div>
        </div>
      </div>
    </section>
  );
}

function RoomHeader({ room, actions }: { room: Room; actions?: ReactNode }) {
  const Icon = KIND_ICONS[room.kind] ?? Bot;
  const members = Array.isArray(room.members) ? room.members : [];
  return (
    <header className="flex shrink-0 flex-wrap items-start gap-3 border-b border-border px-4 py-3" data-testid="room-header">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="inline-flex items-center gap-1 font-medium uppercase tracking-[0.09em]" data-testid="room-kind">
            <Icon className="size-3.5" aria-hidden />
            {ROOM_KIND_LABELS[room.kind] ?? ROOM_LABEL.title}
          </span>
          {room.paused && (
            <span
              className="inline-flex items-center gap-1 rounded-full border border-warning/40 bg-warning/10 px-1.5 py-px text-[10.5px] font-medium text-warning"
              data-testid="room-paused"
              title="Scheduled runs are paused"
            >
              <PauseCircle className="size-3" aria-hidden />
              Paused
            </span>
          )}
          {room.archived && (
            <span
              className="inline-flex items-center gap-1 rounded-full border border-border bg-muted px-1.5 py-px text-[10.5px] font-medium"
              data-testid="room-archived"
            >
              <Archive className="size-3" aria-hidden />
              Archived
            </span>
          )}
        </div>
        <h1 className="mt-0.5 truncate text-base font-semibold tracking-tight text-foreground">{room.title}</h1>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          {members.length > 0 && (
            <span className="inline-flex items-center gap-1.5" data-testid="room-members">
              <span className="flex -space-x-1.5" aria-hidden>
                {members.slice(0, 4).map((m) => (
                  <span
                    key={m.userId}
                    className="flex size-5 items-center justify-center rounded-full border border-background bg-muted text-[9.5px] font-semibold text-foreground/80"
                    title={m.displayName}
                  >
                    {initials(m.displayName)}
                  </span>
                ))}
              </span>
              <span className="truncate">{members.map((m) => m.displayName).join(", ")}</span>
            </span>
          )}
          {room.kind === "report" && room.runsAs?.displayName && (
            <span
              className="inline-flex items-center gap-1"
              data-testid="room-runs-as"
              title="Scheduled runs use this person's permissions, checked again each time it runs"
            >
              <UserRound className="size-3.5" aria-hidden />
              Runs as {room.runsAs.displayName}
            </span>
          )}
        </div>
      </div>
      {actions && (
        <div className="flex shrink-0 items-center gap-2" data-slot="header-actions">
          {actions}
        </div>
      )}
    </header>
  );
}

function initials(name: string): string {
  const parts = (name || "?").trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts.length > 1 ? (parts.at(-1)?.[0] ?? "") : "")).toUpperCase();
}

function NoEdition({ room }: { room: Room }) {
  const when = formatWhen(room.nextRunAt);
  return (
    <div
      className="rounded-xl border border-dashed border-border px-4 py-8 text-center"
      data-testid="room-no-edition"
    >
      <p className="text-sm font-medium text-foreground">No edition yet</p>
      <p className="mx-auto mt-1 max-w-sm text-xs leading-relaxed text-muted-foreground">
        {room.paused
          ? "This report is paused, so no edition will arrive until it is resumed."
          : when
            ? `The first edition arrives ${when}.`
            : "The first edition arrives after the report's first scheduled run."}
      </p>
    </div>
  );
}

function PastEditions({ editions, onPick }: { editions: Edition[]; onPick: (edition: Edition) => void }) {
  return (
    <section aria-labelledby="room-past-editions-label" data-testid="room-past-editions">
      <h3
        id="room-past-editions-label"
        className="pb-1.5 text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted-foreground/70"
      >
        Past editions
      </h3>
      <ul className="divide-y divide-border rounded-lg border border-border">
        {editions.map((edition) => (
          <li key={edition.id}>
            <button
              type="button"
              onClick={() => onPick(edition)}
              className="flex w-full items-baseline gap-3 px-3 py-2 text-left text-sm transition-colors hover:bg-muted/60 focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              data-testid={`room-past-edition-${edition.id}`}
            >
              <span className="min-w-0 flex-1 truncate text-foreground">{edition.title || "Untitled edition"}</span>
              <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                {formatPeriod(edition.periodStart, edition.periodEnd)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function RoomUnavailable({ onLeave }: { onLeave: () => void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center" data-testid="room-unavailable">
      <p className="text-sm font-medium text-foreground">This {ROOM_LABEL.singular} isn&apos;t available</p>
      <p className="max-w-sm text-xs leading-relaxed text-muted-foreground">
        It may have been removed, or it hasn&apos;t been shared with you.
      </p>
      <a
        href={MAIN_CONVERSATION_LINK}
        onClick={(event) => {
          event.preventDefault();
          onLeave();
        }}
        className="mt-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-accent"
      >
        Back to the main conversation
      </a>
    </div>
  );
}
