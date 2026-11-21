"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAppQueryClient } from "@/lib/query/query-client";
import { refusalOf } from "@/lib/query/fetch-refused";
import { useEventStream, type StreamEvent } from "@/lib/event-bus/use-event-stream";
import { ROOMS_ENDPOINT, fetchRooms, postRoomRead } from "@/lib/rooms/api";
import { isRoomEventType, roomIdOf } from "@/lib/rooms/events";
import type { RoomSummary } from "@/lib/rooms/types";

/** The cache entry for the caller's rooms, shared by every reader (rail, badges). */
export const ROOMS_QUERY_KEY = ["rooms", "mine"] as const;

const STALE_MS = 30_000;

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
    },
    client,
  );
  const rooms = useMemo(() => query.data ?? [], [query.data]);
  const error = refusalOf(query.error);

  const { refetch } = query;
  const refresh = useCallback(async () => {
    await refetch();
  }, [refetch]);

  useNewRoomEvents(live, () => {
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
    totalUnread,
    isLoading: query.isPending,
    error: error ? { endpoint: ROOMS_ENDPOINT, status: error.status } : null,
    refresh,
    markRead,
  };
}

/**
 * Calls `onEvent` once for each `room.*` event that arrives after mount. The
 * replay a new stream connection starts with is what happened BEFORE this
 * reader was looking; it is marked seen, not handed over.
 */
function useNewRoomEvents(enabled: boolean, onEvent: (event: StreamEvent) => void) {
  const { events } = useEventStream({ streams: ["agent"], enabled, maxEvents: 50 });
  const seen = useRef<Set<string> | null>(null);
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  });

  useEffect(() => {
    if (!enabled) return;
    if (seen.current === null) {
      seen.current = new Set(events.map((e) => `${e.stream}:${e.id}`));
      return;
    }
    // `events` is newest first; hand them over oldest first.
    const fresh = events.filter((e) => !seen.current!.has(`${e.stream}:${e.id}`)).reverse();
    for (const event of fresh) {
      seen.current.add(`${event.stream}:${event.id}`);
      if (isRoomEventType(event.type)) handler.current(event);
    }
    if (seen.current.size > 500) seen.current = new Set(events.map((e) => `${e.stream}:${e.id}`));
  }, [events, enabled]);
}

/** One room's `room.*` events, as they arrive, for the room that is open. */
export function useRoomEvents(roomId: string | null | undefined, onEvent: (event: StreamEvent) => void) {
  useNewRoomEvents(Boolean(roomId), (event) => {
    if (roomId && roomIdOf(event) === roomId) onEvent(event);
  });
}
