"use client";

import { useCallback, useState, type KeyboardEvent } from "react";

/**
 * Arrow-key movement through a filtered list that keeps focus in its input
 * (the combobox pattern): ArrowDown and ArrowUp move a highlight and wrap at
 * the ends, Home and End jump, Enter picks the highlighted item. `resetKey`
 * (the query) puts the highlight back on the first match whenever it changes.
 */
export function useListNavigation(count: number, resetKey: unknown) {
  // The highlight remembers which query it was set under; a new query reads 0.
  const [state, setState] = useState({ key: resetKey, index: 0 });
  const active = Object.is(state.key, resetKey) ? state.index : 0;
  const setActive = useCallback((index: number) => setState({ key: resetKey, index }), [resetKey]);

  // The list can shrink under the highlight as the query narrows it.
  const current = count === 0 ? -1 : Math.min(active, count - 1);

  const onKeyDown = useCallback(
    (e: KeyboardEvent, pick: (index: number) => void) => {
      if (count === 0) return;
      const moves: Record<string, () => number> = {
        ArrowDown: () => (current + 1) % count,
        ArrowUp: () => (current - 1 + count) % count,
        Home: () => 0,
        End: () => count - 1,
      };
      if (e.key in moves) {
        e.preventDefault();
        setActive(moves[e.key]());
      } else if (e.key === "Enter" && current >= 0) {
        e.preventDefault();
        pick(current);
      }
    },
    [count, current, setActive],
  );

  return { active: current, setActive, onKeyDown };
}
