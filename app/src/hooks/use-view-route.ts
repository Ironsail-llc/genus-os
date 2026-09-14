"use client";

import { useCallback, useEffect, useState } from "react";
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

function toParams(search: string): URLSearchParams {
  return new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
}

/** Unknown values fall back to the default rather than showing a blank shell. */
export function parseViewRoute(search: string): ViewRoute {
  const params = toParams(search);
  const rawView = params.get(VIEW_PARAM) ?? "";
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

  useEffect(() => {
    const sync = () => setRoute(parseViewRoute(window.location.search));
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);

  const navigate = useCallback((view: ViewId, settingsPage?: SettingsPageId) => {
    setRoute((prev) => {
      const next: ViewRoute = {
        view,
        settingsPage: settingsPage ?? prev.settingsPage,
      };
      if (typeof window !== "undefined") {
        const search = formatViewRoute(next, window.location.search);
        window.history.pushState(null, "", `${window.location.pathname}${search}`);
      }
      return next;
    });
  }, []);

  return { ...route, navigate };
}
