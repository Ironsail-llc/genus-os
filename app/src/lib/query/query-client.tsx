"use client";

/**
 * The dashboard's one request cache.
 *
 * Before this, every hook kept its own `useState` and its own `fetch`, so the
 * shell's sidebar badges, the chat landing and the (mounted, hidden) Dashboard
 * view each asked the bridge for the same tasks and agents: three requests and
 * three 60 s timers for one figure. Everything read through a query now shares
 * one in-flight request and one beat per key.
 *
 * Defaults, and why:
 * * `refetchIntervalInBackground: false` — a tab nobody is looking at does not
 *   poll. The next tick after the tab returns picks up where it left off.
 * * `refetchOnWindowFocus: true` — but only for data past its `staleTime`, so
 *   alt-tabbing back reads once, not once per reader.
 * * `retry: false` — every view already renders its own error state; a silent
 *   retry would only delay it.
 * * `staleTime` is set per resource, next to its query.
 */

import {
  QueryClient,
  QueryClientContext,
  QueryClientProvider,
  focusManager,
  notifyManager,
} from "@tanstack/react-query";
import { useContext, useEffect, useState, type ReactNode } from "react";

// An answer reaches its readers in the same task it arrived in, as a hook's own
// setState did, rather than one macrotask later. React batches the renders.
notifyManager.setScheduler(queueMicrotask);

// Tab visibility is read where the browser announces it, on `document`, like
// every other visibility listener in the app. With no argument the manager
// asks `document.visibilityState` itself, so hidden and shown both land.
focusManager.setEventListener((handleFocus) => {
  if (typeof document === "undefined") return;
  const onVisibilityChange = () => handleFocus();
  document.addEventListener("visibilitychange", onVisibilityChange);
  return () => document.removeEventListener("visibilitychange", onVisibilityChange);
});

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        refetchIntervalInBackground: false,
        refetchOnWindowFocus: true,
      },
    },
  });
}

/** Mounted once, by `AppShell`. Re-uses a client already above it. */
export function QueryProvider({ children }: { children: ReactNode }) {
  const parent = useContext(QueryClientContext);
  const [client] = useState(() => parent ?? makeQueryClient());
  if (parent) return <>{children}</>;
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/**
 * The client a data hook reads through: the shell's, or — for a component
 * rendered on its own (a unit test, a page outside the shell) — one of its
 * own. Outside the provider each reader therefore fetches for itself, which is
 * what every reader did before the cache existed.
 */
export function useAppQueryClient(): QueryClient {
  const shared = useContext(QueryClientContext);
  const [own] = useState(() => (shared ? null : makeQueryClient()));
  useEffect(() => {
    if (!own) return;
    own.mount();
    return () => own.unmount();
  }, [own]);
  return shared ?? (own as QueryClient);
}
