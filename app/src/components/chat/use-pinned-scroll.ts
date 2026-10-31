"use client";

import { useCallback, useLayoutEffect, useRef, type DependencyList } from "react";

/** How close to the bottom (px) still counts as reading the newest text. */
export const PINNED_THRESHOLD_PX = 80;

/**
 * Keeps a scroll container at its bottom while, and only while, the reader is
 * there.
 *
 * `follow` are the values whose change can grow the content (messages, the
 * streaming text); after each change the container jumps to the bottom if the
 * reader was within {@link PINNED_THRESHOLD_PX} of it. A reader who has
 * scrolled up is left alone until they come back down. `repin` changing (the
 * reader sent a message) puts them back at the bottom wherever they were.
 *
 * The jump is instant: a smooth scroll restarted on every throttled delta never
 * finishes, and is the thing that dragged readers back down.
 */
export function usePinnedScroll<T extends HTMLElement>(follow: DependencyList, repin: unknown) {
  const containerRef = useRef<T>(null);
  const pinned = useRef(true);

  const onScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight <= PINNED_THRESHOLD_PX;
  }, []);

  const toBottom = useCallback(() => {
    const el = containerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  useLayoutEffect(() => {
    pinned.current = true;
    toBottom();
  }, [repin, toBottom]);

  useLayoutEffect(() => {
    if (pinned.current) toBottom();
    // The caller's list is the dependency list, as with any effect hook.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, follow);

  return { containerRef, onScroll };
}
