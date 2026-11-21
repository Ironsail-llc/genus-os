"use client";

import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchEdition } from "@/lib/rooms/api";
import { useAppQueryClient } from "@/lib/query/query-client";
import type { Edition } from "@/lib/rooms/types";

const POLL_MS = 4_000;
const MAX_FAILED_READS = 5;

/**
 * One edition being built, read again every few seconds until it is
 * published or has failed. The room's `room.edition` event usually brings it
 * sooner (the room view reads its editions again, which also refreshes this:
 * the key sits under the room's editions key); this is the fallback when no
 * event arrives. Five failed reads in a row stop it.
 */
export function useEditionWatch(roomId: string, editionId: string | null): Edition | null {
  const client = useAppQueryClient();
  const query = useQuery(
    {
      queryKey: ["rooms", roomId, "editions", "watch", editionId],
      queryFn: ({ signal }) => fetchEdition(roomId, editionId as string, signal),
      enabled: Boolean(editionId),
      staleTime: 0,
      refetchInterval: (q) => {
        if (q.state.errorUpdateCount >= MAX_FAILED_READS && q.state.status === "error") return false;
        const status = q.state.data?.status;
        return status === "published" || status === "failed" ? false : POLL_MS;
      },
    },
    client,
  );
  return editionId ? (query.data ?? null) : null;
}

/** The failure an edition carries, when the server sent one. */
export function editionError(edition: Edition | null): string | null {
  const error = (edition as { error?: unknown } | null)?.error;
  return typeof error === "string" && error.trim() ? error.trim() : null;
}

/** An edition someone here asked for: by id, or (no id given) the next one after `afterSeq`. */
export interface EditionRequest {
  /** A new number for each request, so a repeat is a new request. */
  key: number;
  id: string | null;
  afterSeq: number;
}

/**
 * Follows a requested edition until it is among the room's published ones,
 * then shows it once. The poll (`useEditionWatch`) finding it published reads
 * the room's editions again; finding it failed ends the wait with the
 * failure. Everything is derived from the request and what has arrived, so a
 * newer request simply replaces an older one.
 */
export function useEditionArrival(
  roomId: string,
  request: EditionRequest | null,
  editions: readonly Edition[],
  showEdition: (edition: Edition) => void,
  refresh: () => void,
): { waiting: boolean; failed: Edition | null } {
  const watched = useEditionWatch(roomId, request?.id ?? null);
  const arrived = request
    ? (editions.find((e) => (request.id ? e.id === request.id : e.seq > request.afterSeq)) ?? null)
    : null;
  const current = watched && request?.id && watched.id === request.id ? watched : null;
  const failed = !arrived && current?.status === "failed" ? current : null;

  const shownFor = useRef<number | null>(null);
  useEffect(() => {
    if (!request || !arrived || shownFor.current === request.key) return;
    shownFor.current = request.key;
    showEdition(arrived);
  }, [request, arrived, showEdition]);

  const refreshedFor = useRef<number | null>(null);
  useEffect(() => {
    if (!request || arrived || current?.status !== "published" || refreshedFor.current === request.key) return;
    refreshedFor.current = request.key;
    refresh();
  }, [request, arrived, current, refresh]);

  return { waiting: Boolean(request) && !arrived && !failed, failed };
}
