"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ALL_VIEW_IDS,
  settingsPages,
  type SettingsPageId,
  type ViewId,
} from "@/components/layout/nav-config";
import { isLogSince } from "@/lib/observe/log-since";
import { isRoomId } from "@/lib/rooms/room-id";

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
  /**
   * A workspace (room) open on the chat view: `?v=chat&room={id}`, the link
   * every room tool answers with. Absent — the key itself, not just its value —
   * for the main conversation, so that route reads exactly as it always has.
   */
  room?: string;
  /**
   * A search handed to the Logs page: `?v=logs&q={query}`, the link the run
   * drawer's "Logs for this run" opens. Absent, the key itself, for every
   * other route.
   */
  query?: string;
  /**
   * The window that search opens on (`&since=`), a relative age or an
   * ISO-8601 timestamp as the bridge reads them: a run's link opens a little
   * before the run started. Only with a query; absent otherwise.
   */
  since?: string;
}

const VIEW_PARAM = "v";
const SUB_PARAM = "s";
const ROOM_PARAM = "room";
const QUERY_PARAM = "q";
const SINCE_PARAM = "since";

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

  // Only the chat view opens a room, and only an id of a room id's shape.
  const rawRoom = params.get(ROOM_PARAM);
  if (view === "chat" && isRoomId(rawRoom)) return { view, settingsPage, room: rawRoom };

  // Only the logs view reads a search, and only a non-empty one.
  const rawQuery = params.get(QUERY_PARAM);
  if (view === "logs" && rawQuery) {
    const rawSince = params.get(SINCE_PARAM);
    return rawSince && isLogSince(rawSince)
      ? { view, settingsPage, query: rawQuery, since: rawSince }
      : { view, settingsPage, query: rawQuery };
  }

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
  // Written for the room that is open and dropped otherwise: leaving a room,
  // or the chat view, is going back to the main conversation.
  if (route.view === "chat" && route.room && isRoomId(route.room)) {
    params.set(ROOM_PARAM, route.room);
  } else {
    params.delete(ROOM_PARAM);
  }
  if (route.view === "logs" && route.query) {
    params.set(QUERY_PARAM, route.query);
  } else {
    params.delete(QUERY_PARAM);
  }
  if (route.view === "logs" && route.query && route.since) {
    params.set(SINCE_PARAM, route.since);
  } else {
    params.delete(SINCE_PARAM);
  }
  return `?${params.toString()}`;
}

/** The link to the Logs page with `query` prefilled and run (over `since`), for an `href`. */
export function logsLink(query: string, since?: string): string {
  return formatViewRoute({ view: "logs", settingsPage: DEFAULT_SETTINGS_PAGE, query, since });
}

export interface ViewRouteApi extends ViewRoute {
  navigate: (view: ViewId, settingsPage?: SettingsPageId) => void;
  /** Open a workspace on the chat view, or (null) go back to the main conversation. */
  openRoom: (roomId: string | null) => void;
  /** Open the Logs page with `query` prefilled and run, over `since` when given. */
  openLogs: (query: string, since?: string) => void;
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

  const go = useCallback(
    (next: ViewRoute) => {
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

  const navigate = useCallback(
    (view: ViewId, settingsPage?: SettingsPageId) =>
      go({ view, settingsPage: settingsPage ?? routeRef.current.settingsPage }),
    [go]
  );

  const openRoom = useCallback(
    (roomId: string | null) => {
      const settingsPage = routeRef.current.settingsPage;
      go(roomId && isRoomId(roomId) ? { view: "chat", settingsPage, room: roomId } : { view: "chat", settingsPage });
    },
    [go]
  );

  const openLogs = useCallback(
    (query: string, since?: string) =>
      go({ view: "logs", settingsPage: routeRef.current.settingsPage, query, since }),
    [go]
  );

  return { ...route, navigate, openRoom, openLogs };
}
