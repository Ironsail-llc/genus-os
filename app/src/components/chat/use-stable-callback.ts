"use client";

import { useCallback, useLayoutEffect, useRef } from "react";

/**
 * A callback whose identity never changes but which always calls the latest
 * `fn`. Memoised children (the chat's message bubbles) take it as a prop
 * without being re-rendered each time the parent's closure changes.
 */
export function useStableCallback<A extends unknown[], R>(fn: (...args: A) => R): (...args: A) => R {
  const ref = useRef(fn);
  useLayoutEffect(() => {
    ref.current = fn;
  });
  return useCallback((...args: A) => ref.current(...args), []);
}
