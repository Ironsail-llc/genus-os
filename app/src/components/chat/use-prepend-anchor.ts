"use client";

import { useCallback, useLayoutEffect, useRef, type RefObject } from "react";

/**
 * Keeps the reader's place when messages are added above them ("Load
 * earlier", or a "Show earlier" window opening).
 *
 * A prepend is the first rendered message changing while the last stays the
 * same. After one, the container is scrolled so the content that was below
 * the top of the view is still the same distance from the bottom. Safari has
 * no CSS scroll anchoring, so this is done by hand everywhere.
 *
 * The distance is recorded after every render and on every scroll (wire the
 * returned callback into the container's `onScroll`). Declare it after
 * `usePinnedScroll`: its layout effect runs later and wins for a prepend.
 */
export function usePrependAnchor<T extends HTMLElement>(
  ref: RefObject<T | null>,
  firstId: string | undefined,
  lastId: string | undefined,
) {
  const saved = useRef<{ first?: string; last?: string; fromBottom: number } | null>(null);

  const remember = useCallback(() => {
    const el = ref.current;
    if (el && saved.current) saved.current.fromBottom = el.scrollHeight - el.scrollTop;
  }, [ref]);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const prev = saved.current;
    if (prev && prev.first !== undefined && firstId !== prev.first && lastId === prev.last) {
      el.scrollTop = el.scrollHeight - prev.fromBottom;
    }
    saved.current = { first: firstId, last: lastId, fromBottom: el.scrollHeight - el.scrollTop };
  });

  return remember;
}
