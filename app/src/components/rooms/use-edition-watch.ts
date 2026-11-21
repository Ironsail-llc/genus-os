"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery, type QueryClient } from "@tanstack/react-query";
import { fetchEdition } from "@/lib/rooms/api";
import { useAppQueryClient } from "@/lib/query/query-client";
import type { Edition } from "@/lib/rooms/types";

const POLL_MS = 4_000;
const MAX_FAILED_READS = 5;
const LIST_POLL_MS = 4_000;

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

/** How long a request is waited for before it is called "still building". */
export const EDITION_WAIT_MS = 3 * 60_000;

/**
 * Starts the watch from what a request was answered with, so a published
 * answer is shown without another read (call before setting the request).
 */
export function seedEditionWatch(client: QueryClient, roomId: string, edition: Edition) {
  client.setQueryData(["rooms", roomId, "editions", "watch", edition.id], edition);
}

function editionsKey(roomId: string) {
  return ["rooms", roomId, "editions"] as const;
}

/** Puts an edition into the room's loaded editions (newest first), once. */
function adopt(client: QueryClient, roomId: string, edition: Edition) {
  client.setQueryData<Edition[]>(editionsKey(roomId), (prev) => {
    const list = prev ?? [];
    if (list.some((e) => e.id === edition.id)) return prev;
    return [...list, edition].sort((a, b) => (b.seq ?? 0) - (a.seq ?? 0));
  });
}

/**
 * Follows a requested edition until it can be shown, then shows it once.
 *
 * The edition is read by id (`GET /editions/{id}`), never only looked for in
 * the room's loaded list, which holds just the newest editions: a stored
 * edition of an old period, or one published after the list loaded, is put
 * into the room's editions (where the room view pins from) and shown, and
 * kept there if a later read of the list leaves it out again.
 *
 * The wait always ends: published, shown; failed, with its reason; or, after
 * `EDITION_WAIT_MS`, "still building" (`timedOut`), with the control usable
 * again. A request that named no edition reads the room's editions again
 * every few seconds instead (the event stream may be down).
 */
export function useEditionArrival(
  roomId: string,
  request: EditionRequest | null,
  editions: readonly Edition[],
  showEdition: (edition: Edition) => void,
  refresh: () => void,
): { waiting: boolean; failed: Edition | null; timedOut: boolean } {
  const client = useAppQueryClient();
  const watched = useEditionWatch(roomId, request?.id ?? null);
  const current = watched && request?.id && watched.id === request.id ? watched : null;
  const inList = request
    ? (editions.find((e) => (request.id ? e.id === request.id : e.seq > request.afterSeq)) ?? null)
    : null;
  const ready = inList ?? (current?.status === "published" && current.preview !== true ? current : null);
  const failed = !ready && current?.status === "failed" ? current : null;

  // Published but not in the loaded list: put it there (again, if a later
  // read of the list dropped it), so the room view can pin it.
  const adoptedFor = useRef<{ key: number; edition: Edition } | null>(null);
  useEffect(() => {
    if (request && ready && !inList) {
      adoptedFor.current = { key: request.key, edition: ready };
      adopt(client, roomId, ready);
    }
  }, [client, roomId, request, ready, inList]);
  useEffect(() => {
    const kept = adoptedFor.current;
    if (kept && !editions.some((e) => e.id === kept.edition.id)) adopt(client, roomId, kept.edition);
  }, [client, roomId, editions]);

  const shownFor = useRef<number | null>(null);
  useEffect(() => {
    if (!request || !inList || shownFor.current === request.key) return;
    shownFor.current = request.key;
    showEdition(inList);
  }, [request, inList, showEdition]);

  const [expiredKey, setExpiredKey] = useState<number | null>(null);
  const settled = Boolean(ready) || Boolean(failed);
  useEffect(() => {
    if (!request || settled) return;
    const left = Math.max(0, request.key + EDITION_WAIT_MS - Date.now());
    const timer = setTimeout(() => setExpiredKey(request.key), left);
    return () => clearTimeout(timer);
  }, [request, settled]);
  const timedOut = Boolean(request) && !settled && expiredKey === request?.key;

  // No edition named: nothing to read by id, so read the room's editions
  // again until a newer one arrives, or the wait runs out.
  const waitingUnnamed = Boolean(request) && !request?.id && !inList && !timedOut;
  useEffect(() => {
    if (!waitingUnnamed) return;
    const timer = setInterval(refresh, LIST_POLL_MS);
    return () => clearInterval(timer);
  }, [waitingUnnamed, refresh]);

  return { waiting: Boolean(request) && !inList && !failed && !timedOut, failed, timedOut };
}
