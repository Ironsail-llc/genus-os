"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { readBridgeReply } from "@/lib/bridge/read-reply";
import {
  normalizePending,
  routeCount,
  type AnswerBody,
  type AnswerOutcome,
  type PendingItem,
  type PendingKind,
} from "@/lib/inbox/pending";

/**
 * The one reader of `GET /api/approvals` in the Helm.
 *
 * It is mounted ONCE, by `AppShell`, and feeds both the Inbox view and the
 * Inbox badge in the sidebar and the phone tab bar. Two pollers on one
 * operator-gated route would double the traffic to say the same number twice,
 * and would let the badge and the list disagree about what is waiting — which
 * is the one thing a badge exists to prevent.
 *
 * `active` is whether the Inbox view is the one on screen, and it sets the
 * CADENCE rather than switching the poll on and off. The badge exists to tell
 * the operator something is waiting while they are somewhere else, so a poll
 * that stopped off-screen would leave the number frozen at whatever it was on
 * page load — the one thing a badge must not do. Thirty seconds while the
 * Inbox is being read, sixty while it is not: the slower rate still keeps the
 * badge honest without waking an operator-gated route twice a minute behind a
 * view nobody has open.
 *
 * A HIDDEN DOCUMENT stops it completely. A backgrounded tab's answers are read
 * by nobody, and a laptop left open for a day would otherwise spend it polling
 * the bridge. Returning to the tab refreshes at once rather than showing a
 * stale badge until the next tick.
 *
 * Role gating in the view is UX only: the bridge calls `require_operator` on
 * the listing and on every answer.
 */

const BRIDGE = "/api/bridge";

/** While the Inbox is the view being read. */
const ACTIVE_POLL_MS = 30_000;
/** While the operator is elsewhere — slower, but never stopped. */
const BACKGROUND_POLL_MS = 60_000;

function documentHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export interface UseInboxOptions {
  /** Whether the Inbox view is the one on screen. */
  active?: boolean;
}

export interface UseInboxApi {
  items: PendingItem[];
  /** What the ROUTE says is waiting — see `routeCount`. This is the badge. */
  count: number;
  /** `count` minus the rows that actually rendered. Normally 0. */
  unrenderable: number;
  isLoading: boolean;
  /** A listing that failed for a reason that is not "this is not your screen". */
  error: string | null;
  /** The bridge said the listing is the operator's. Not a broken appliance. */
  refusedAsNonOperator: boolean;
  refresh: () => Promise<void>;
  answer: (id: string, kind: PendingKind, body: AnswerBody) => Promise<AnswerOutcome>;
}

export function useInbox({ active = false }: UseInboxOptions = {}): UseInboxApi {
  const [items, setItems] = useState<PendingItem[]>([]);
  const [count, setCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refusedAsNonOperator, setRefusedAsNonOperator] = useState(false);

  // A poll that started before a newer one must not overwrite the newer
  // answer — an operator who just settled a card would see it flicker back.
  const loadIdRef = useRef(0);

  /**
   * Rows this session has settled, which the route may not have caught up with.
   *
   * `loadIdRef` above only orders loads against OTHER loads. It says nothing
   * about the case the 30 s interval actually produces: a GET that left BEFORE
   * an answer and lands AFTER it, carrying a listing minted while the row was
   * still pending. Without this memo that answered card comes back — with its
   * Approve and Reject buttons — and the badge goes up with it, until the next
   * tick up to a minute later. Clicking it again gets "Already decided".
   *
   * An id is forgotten as soon as the route stops listing it, so the set
   * tracks the route's lag rather than growing for the life of the session,
   * and a genuinely new row that reuses an id is shown again.
   */
  const settledRef = useRef<Set<string>>(new Set());

  /** Whether anything has been read yet; the spinner is for that and for Refresh. */
  const hasLoadedRef = useRef(false);

  const load = useCallback(async (options: { silent?: boolean } = {}) => {
    const id = ++loadIdRef.current;
    // A background tick must not blank a populated screen: the view swaps the
    // empty state for a spinner whenever the list is empty, so an unconditional
    // setIsLoading(true) made an empty inbox blink every 30 seconds.
    const quiet = options.silent === true && hasLoadedRef.current;
    if (!quiet) setIsLoading(true);
    try {
      const res = await fetch(`${BRIDGE}/api/approvals`);
      if (id !== loadIdRef.current) return;

      if (!res.ok) {
        if (res.status === 403) {
          setRefusedAsNonOperator(true);
          setItems([]);
          setCount(0);
          setError(null);
          return;
        }
        setError(await readBridgeReply(res));
        return;
      }

      const body: unknown = await res.json();
      const fresh = normalizePending(body);
      const visible = fresh.filter((row) => !settledRef.current.has(row.id));

      // Forget ids the route has stopped listing: the memo exists to cover its
      // lag, not to blacklist an id for the life of the session.
      const listed = new Set(fresh.map((row) => row.id));
      for (const settledId of settledRef.current) {
        if (!listed.has(settledId)) settledRef.current.delete(settledId);
      }

      setRefusedAsNonOperator(false);
      setItems(visible);
      // The route's own count, less the settled rows it is still listing.
      setCount(Math.max(0, routeCount(body, fresh.length) - (fresh.length - visible.length)));
      setError(null);
    } catch {
      if (id !== loadIdRef.current) return;
      setError("The dashboard could not reach the bridge. Check that the service is running.");
    } finally {
      if (id === loadIdRef.current) {
        hasLoadedRef.current = true;
        if (!quiet) setIsLoading(false);
      }
    }
  }, []);

  // What `active` was the last time this effect ran. Without it, LEAVING the
  // Inbox would fetch on the way out — the effect re-runs on every change of
  // `active`, and a bare `load()` in its body cannot tell arriving from going.
  const wasActiveRef = useRef<boolean | null>(null);

  useEffect(() => {
    const firstMount = wasActiveRef.current === null;
    const justOpened = active && wasActiveRef.current === false;
    wasActiveRef.current = active;

    if (firstMount || justOpened) void load();

    let timer: ReturnType<typeof setInterval> | null = null;

    const stop = () => {
      if (timer !== null) clearInterval(timer);
      timer = null;
    };

    const start = () => {
      stop();
      if (documentHidden()) return;
      timer = setInterval(
        () => void load({ silent: true }),
        active ? ACTIVE_POLL_MS : BACKGROUND_POLL_MS
      );
    };

    const onVisibilityChange = () => {
      if (documentHidden()) {
        stop();
        return;
      }
      // The tab was dark for an unknown length of time, so the count in the
      // badge is of unknown age: read it again before starting the clock.
      void load({ silent: true });
      start();
    };

    start();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [active, load]);

  const answer = useCallback(
    async (id: string, kind: PendingKind, body: AnswerBody): Promise<AnswerOutcome> => {
      let res: Response;
      try {
        res = await fetch(`${BRIDGE}/api/approvals/${kind}/${encodeURIComponent(id)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch {
        return {
          settled: false,
          message: "The dashboard could not reach the bridge to send that.",
        };
      }

      if (!res.ok) return { settled: false, message: await readBridgeReply(res) };

      const payload = (await res.json()) as { settled?: unknown; message?: unknown };
      if (payload?.settled !== true) {
        return {
          settled: false,
          message:
            typeof payload?.message === "string" && payload.message
              ? payload.message
              : "The bridge did not settle that.",
        };
      }

      // Dropped locally rather than waited for: the next poll is up to thirty
      // seconds away, and a card that stays put after a successful answer
      // reads as a failure. Recorded in the memo FIRST, so a GET that is
      // already on the wire cannot land and put it back.
      settledRef.current.add(id);
      setItems((prev) => prev.filter((item) => item.id !== id));
      setCount((prev) => Math.max(0, prev - 1));
      return { settled: true, message: null };
    },
    []
  );

  return {
    items,
    count,
    unrenderable: Math.max(0, count - items.length),
    isLoading,
    error,
    refusedAsNonOperator,
    refresh: load,
    answer,
  };
}
