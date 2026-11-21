"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAppQueryClient } from "@/lib/query/query-client";
import { FetchRefused, refusalOf } from "@/lib/query/fetch-refused";
import { useEventStream, type StreamEvent } from "@/lib/event-bus/use-event-stream";
import { ROOMS_ENDPOINT, fetchRooms, postRoomRead } from "@/lib/rooms/api";
import { isRoomEventType, roomIdOf } from "@/lib/rooms/events";
import type { RoomSummary } from "@/lib/rooms/types";

/** The cache entry for the caller's rooms, shared by every reader (rail, badges). */
export const ROOMS_QUERY_KEY = ["rooms", "mine"] as const;

const STALE_MS = 30_000;

/** A 404 for the list itself: nothing serves rooms here (yet). Not an error to show. */
function notServed(error: unknown): boolean {
  return error instanceof FetchRefused && error.status === 404;
}

interface UseRoomsOptions {
  /** Subscribe to the agent stream and read the list again on `room.*` events. */
  live?: boolean;
}

/**
 * The calling user's rooms (`GET /api/rooms` through the bridge proxy), with
 * their unread counts.
 *
 * The events stream has already dropped other tenants' events server-side
 * (`lib/event-bus/tenant-filter.ts`). A `room.*` event for a room not in the
 * list still reads the list again, because "you were just given a room" looks
 * exactly like that; nothing else of it is shown.
 */
export function useRooms(options: UseRoomsOptions = {}) {
  const { live = false } = options;
  const client = useAppQueryClient();
  const query = useQuery(
    {
      queryKey: ROOMS_QUERY_KEY,
      queryFn: ({ signal }) => fetchRooms(signal),
      staleTime: STALE_MS,
      // Not served is an answer, not a blip: it is not asked again until reload.
      refetchOnWindowFocus: (q) => !notServed(q.state.error),
      refetchOnReconnect: (q) => !notServed(q.state.error),
    },
    client,
  );
  const rooms = useMemo(() => query.data ?? [], [query.data]);
  // When the list answers 404 the feature is hidden: no rail, no live
  // subscription, no re-reads. Any other refusal is shown as one.
  const available = !notServed(query.error);
  const error = available ? refusalOf(query.error) : null;

  const { refetch } = query;
  const refresh = useCallback(async () => {
    await refetch();
  }, [refetch]);

  useNewRoomEvents(live && available, () => {
    void client.invalidateQueries({ queryKey: ROOMS_QUERY_KEY });
  });

  /** Opening a room: one POST, and this user's badge for it clears at once. */
  const markRead = useCallback(
    async (roomId: string) => {
      client.setQueryData<RoomSummary[]>(ROOMS_QUERY_KEY, (prev) =>
        prev?.map((room) => (room.id === roomId ? { ...room, unread: 0 } : room)),
      );
      await postRoomRead(roomId);
    },
    [client],
  );

  const totalUnread = useMemo(
    () => rooms.reduce((sum, room) => sum + (room.archived ? 0 : Math.max(0, room.unread || 0)), 0),
    [rooms],
  );

  return {
    rooms,
    /** False when nothing serves the rooms list (404): the shell shows no rail. */
    available,
    totalUnread,
    isLoading: query.isPending,
    error: error ? { endpoint: ROOMS_ENDPOINT, status: error.status } : null,
    refresh,
    markRead,
  };
}

/**
 * Calls `onEvent` once for each `room.*` event that arrives after the stream
 * has synced. A new connection first replays recent events and then sends its
 * sync marker; what arrives before that marker happened BEFORE this reader
 * was looking, so it is marked seen, not handed over (handing it over read
 * the room list again and posted a spurious read on every open).
 */
function useNewRoomEvents(enabled: boolean, onEvent: (event: StreamEvent) => void) {
  const { events, synced } = useEventStream({ streams: ["agent"], enabled, maxEvents: 50 });
  const seen = useRef<Set<string>>(new Set());
  // Set on the first render after the sync marker; until then, all is replay.
  const armed = useRef(false);
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  });

  useEffect(() => {
    if (!enabled) return;
    const key = (e: StreamEvent) => `${e.stream}:${e.id}`;
    if (!synced || !armed.current) {
      for (const event of events) seen.current.add(key(event));
      armed.current = synced;
      return;
    }
    // `events` is newest first; hand them over oldest first.
    const fresh = events.filter((e) => !seen.current.has(key(e))).reverse();
    for (const event of fresh) {
      seen.current.add(key(event));
      if (isRoomEventType(event.type)) handler.current(event);
    }
    if (seen.current.size > 500) seen.current = new Set(events.map(key));
  }, [events, enabled, synced]);
}

/** One room's `room.*` events, as they arrive, for the room that is open. */
export function useRoomEvents(roomId: string | null | undefined, onEvent: (event: StreamEvent) => void) {
  useNewRoomEvents(Boolean(roomId), (event) => {
    if (roomId && roomIdOf(event) === roomId) onEvent(event);
  });
}
