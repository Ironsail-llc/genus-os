"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api/client";

type Page<T> = { items: T[]; next_cursor: string | null };
type State<T> = Page<T> & { path: string; loading: boolean; error: string | null };
const empty = { items: [], next_cursor: null, loading: true, error: null };

export function useSalesPages<T extends { id: string }>(path: string) {
  const [state, setState] = useState<State<T>>({ ...empty, path });
  const generation = useRef(0);
  const invalidate = useCallback(() => ++generation.current, []);
  useEffect(() => {
    const request = invalidate();
    const controller = new AbortController();
    apiFetch<Page<T>>(path, { signal: controller.signal }).then((page) => {
      if (generation.current === request) setState({ ...page, path, loading: false, error: null });
    }).catch((error) => {
      if (generation.current === request && !controller.signal.aborted)
        setState({ ...empty, path, loading: false, error: String(error) });
    });
    return () => { controller.abort(); invalidate(); };
  }, [path, invalidate]);

  const load = useCallback(async (after?: string) => {
    const request = invalidate();
    setState((old) => ({ ...old, loading: true, error: null }));
    try {
      const page = await apiFetch<Page<T>>(path + (after ? `&after=${encodeURIComponent(after)}` : ""));
      if (request !== generation.current) return;
      setState((old) => ({ ...page, path, loading: false, error: null,
        items: after ? [...new Map([...old.items, ...page.items].map((item) => [item.id, item])).values()] : page.items }));
    } catch (error) {
      if (request === generation.current) setState((old) => ({ ...old, loading: false, error: String(error) }));
    }
  }, [path, invalidate]);

  return { ...(state.path === path ? state : { ...empty, path }), reload: () => load(),
    more: () => load(state.next_cursor ?? undefined) };
}
