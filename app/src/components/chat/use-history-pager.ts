"use client";

import { useCallback, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { historyToMessages, oldestServerId, type HistoryMessage } from "@/lib/chat/history-messages";

/** Rows per "Load earlier": the same page size the first load asks for. */
export const HISTORY_PAGE = 50;

/**
 * "Load earlier": pages back through the stored conversation, past the first
 * 50 messages the panel loads.
 *
 * Pages by cursor (`before=<oldest stored id on screen>`), not offset: new
 * turns land while the reader scrolls back, and an offset counted from the
 * newest row would shift under them and show a page twice. Older messages go
 * above the ones on screen, and any already there are skipped.
 *
 * The panel calls `settle(hasMore)` when its first load answers; only the
 * engine's own `hasMore` turns the button on, so with history pages off
 * nothing is offered. A page that answers after the chat was pointed at
 * another agent is dropped, for the reason the panel's first load is.
 *
 * `M` is the panel's message type; the pages it adds are `HistoryMessage`s,
 * which it must accept.
 */
export function useHistoryPager<M extends { id: string; serverId?: number }>(
  agentQuery: string,
  messages: M[],
  setMessages: Dispatch<SetStateAction<M[]>>,
) {
  // `hasMore` is remembered with the chat it was said about: pointed at another
  // agent, the panel offers nothing until that agent's first page settles.
  const [more, setMore] = useState({ agentQuery, value: false });
  const hasMore = more.agentQuery === agentQuery && more.value;
  const [loading, setLoading] = useState(false);
  const latest = useRef({ agentQuery, messages });
  latest.current = { agentQuery, messages };

  const setHasMore = useCallback((value: boolean) => setMore({ agentQuery: latest.current.agentQuery, value }), []);
  const settle = setHasMore;

  const loadEarlier = useCallback(async () => {
    const asked = latest.current.agentQuery;
    const before = oldestServerId(latest.current.messages);
    if (before === undefined) return;
    setLoading(true);
    try {
      const sep = asked ? `${asked}&` : "?";
      const res = await fetch(`/api/chat/history${sep}before=${before}&limit=${HISTORY_PAGE}`, { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      if (latest.current.agentQuery !== asked) return;
      const older: HistoryMessage[] = historyToMessages(data.messages);
      setMessages((prev) => {
        const onScreen = new Set(prev.map((m) => m.id));
        return [...(older.filter((m) => !onScreen.has(m.id)) as unknown as M[]), ...prev];
      });
      setHasMore(data.hasMore === true);
    } catch {
      // Keep the button: the reader can try again.
    } finally {
      setLoading(false);
    }
  }, [setMessages, setHasMore]);

  return { hasMore, loading, settle, loadEarlier };
}
