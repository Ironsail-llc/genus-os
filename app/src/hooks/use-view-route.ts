"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ALL_VIEW_IDS,
  settingsPages,
  type SettingsPageId,
  type ViewId,
} from "@/components/layout/nav-config";

/**
 * The active view lives in the query string (`?v=<view>&s=<settings page>`) so a
 * reload or a shared link lands on the same screen and back/forward work.
 *
 * Deliberately hand-rolled on `history` + `popstate` rather than
 * next/navigation: the Helm is a single client shell (no App Router routes per
 * view), and this keeps the shell renderable in jsdom without a router
 * provider, which is how every existing test mounts it.
 */

export const DEFAULT_VIEW: ViewId = "chat";
export const DEFAULT_SETTINGS_PAGE: SettingsPageId = settingsPages[0].id;

export interface ViewRoute {
  view: ViewId;
  settingsPage: SettingsPageId;
}

const VIEW_PARAM = "v";
const SUB_PARAM = "s";

const viewIds = new Set<string>(ALL_VIEW_IDS);
const settingsPageIds = new Set<string>(settingsPages.map((p) => p.id));

/**
 * Views that used to have their own id and no longer do. Links minted by the
 * previous Helm keep working instead of silently landing on chat.
 */
const VIEW_ALIASES: Record<string, ViewRoute> = {
  controls: { view: "settings", settingsPage: "flags" },
  canvas: { view: DEFAULT_VIEW, settingsPage: DEFAULT_SETTINGS_PAGE },
};

function toParams(search: string): URLSearchParams {
  return new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
}

/** Unknown values fall back to the default rather than showing a blank shell. */
export function parseViewRoute(search: string): ViewRoute {
  const params = toParams(search);
  const rawView = params.get(VIEW_PARAM) ?? "";

  const alias = VIEW_ALIASES[rawView];
  if (alias) return alias;

  const view = viewIds.has(rawView) ? (rawView as ViewId) : DEFAULT_VIEW;

  const rawSub = params.get(SUB_PARAM) ?? "";
  const settingsPage =
    view === "settings" && settingsPageIds.has(rawSub)
      ? (rawSub as SettingsPageId)
      : DEFAULT_SETTINGS_PAGE;

  return { view, settingsPage };
}

/**
 * Renders a route back into a query string. `currentSearch`, when given, is
 * preserved so unrelated parameters survive navigation.
 */
export function formatViewRoute(route: ViewRoute, currentSearch = ""): string {
  const params = toParams(currentSearch);
  params.set(VIEW_PARAM, route.view);
  if (route.view === "settings") {
    params.set(SUB_PARAM, route.settingsPage);
  } else {
    params.delete(SUB_PARAM);
  }
  return `?${params.toString()}`;
}

export interface ViewRouteApi extends ViewRoute {
  navigate: (view: ViewId, settingsPage?: SettingsPageId) => void;
}

export function useViewRoute(): ViewRouteApi {
  // SSR renders the default; the effect below adopts the real URL on mount so
  // the server and first client render agree.
  const [route, setRoute] = useState<ViewRoute>({
    view: DEFAULT_VIEW,
    settingsPage: DEFAULT_SETTINGS_PAGE,
  });

  // navigate() needs the route that is showing without depending on the render
  // that produced it, so the callback stays stable and the history write stays
  // out of the state updater (updaters must be pure — StrictMode runs them twice).
  const routeRef = useRef(route);

  const applyRoute = useCallback((next: ViewRoute) => {
    routeRef.current = next;
    setRoute(next);
  }, []);

  useEffect(() => {
    const sync = () => applyRoute(parseViewRoute(window.location.search));

    // A link carrying an unknown or retired view is rewritten to where it
    // actually landed, so the bad URL does not survive a copy or a reload. A
    // bare URL is left alone — nothing to correct there.
    const search = window.location.search;
    if (toParams(search).has(VIEW_PARAM)) {
      const canonical = formatViewRoute(parseViewRoute(search), search);
      if (canonical !== search) {
        window.history.replaceState(null, "", `${window.location.pathname}${canonical}`);
      }
    }
    sync();

    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, [applyRoute]);

  const navigate = useCallback(
    (view: ViewId, settingsPage?: SettingsPageId) => {
      const next: ViewRoute = {
        view,
        settingsPage: settingsPage ?? routeRef.current.settingsPage,
      };

      if (typeof window !== "undefined") {
        const search = formatViewRoute(next, window.location.search);
        // Re-selecting the view you are already on must not stack a history
        // entry — otherwise Back does nothing visible for several presses.
        if (search !== window.location.search) {
          window.history.pushState(null, "", `${window.location.pathname}${search}`);
        }
      }

      applyRoute(next);
    },
    [applyRoute]
  );

  return { ...route, navigate };
}
