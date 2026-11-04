"use client";

import { useCallback, useMemo, useState } from "react";

/** How many messages the chat renders before older ones wait behind "Show earlier". */
export const MESSAGE_WINDOW = 200;

/**
 * The trailing window of a conversation that the chat renders.
 *
 * A windowing hook rather than a virtualiser. Bubbles are variable-height
 * markdown, so a virtualiser has to measure them, and its estimates fight the
 * pinned-to-bottom scroll as rows mount and resize; it also takes offscreen
 * messages out of find-in-page and the accessibility tree. History loads at
 * most 50 messages, so the window only ever applies to a long live session, and
 * memoised bubbles plus `content-visibility` already make the rendered ones
 * cheap. This bounds the rest without a dependency.
 *
 * `visible` is the same array while `messages` and the window are unchanged.
 */
export function useMessageWindow<T>(messages: T[], size: number = MESSAGE_WINDOW) {
  // How many windows the reader has asked for, counted back from the newest.
  const [windows, setWindows] = useState(1);
  const shown = windows * size;
  const visible = useMemo(
    () => (messages.length > shown ? messages.slice(messages.length - shown) : messages),
    [messages, shown],
  );
  const showEarlier = useCallback(() => setWindows((n) => n + 1), []);
  return { visible, hiddenCount: messages.length - visible.length, showEarlier };
}
