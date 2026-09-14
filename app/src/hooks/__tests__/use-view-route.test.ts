import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { StrictMode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import {
  DEFAULT_SETTINGS_PAGE,
  DEFAULT_VIEW,
  formatViewRoute,
  parseViewRoute,
  useViewRoute,
} from "../use-view-route";

describe("parseViewRoute", () => {
  it("reads the view out of the query string", () => {
    expect(parseViewRoute("?v=runs")).toEqual({
      view: "runs",
      settingsPage: DEFAULT_SETTINGS_PAGE,
    });
  });

  it("tolerates a query string without the leading ?", () => {
    expect(parseViewRoute("v=fleet").view).toBe("fleet");
  });

  it("reads the settings sub-page", () => {
    expect(parseViewRoute("?v=settings&s=flags")).toEqual({
      view: "settings",
      settingsPage: "flags",
    });
  });

  it("falls back to chat for an unknown view", () => {
    expect(parseViewRoute("?v=not-a-view").view).toBe("chat");
    expect(DEFAULT_VIEW).toBe("chat");
  });

  it("falls back to chat for an empty query string", () => {
    expect(parseViewRoute("").view).toBe("chat");
  });

  it("falls back to the first settings page for an unknown sub-page", () => {
    expect(parseViewRoute("?v=settings&s=nope").settingsPage).toBe(
      DEFAULT_SETTINGS_PAGE
    );
  });

  it("ignores a sub-page on a non-settings view", () => {
    expect(parseViewRoute("?v=runs&s=flags").settingsPage).toBe(
      DEFAULT_SETTINGS_PAGE
    );
  });

  it("keeps links to the retired controls view working", () => {
    expect(parseViewRoute("?v=controls")).toEqual({
      view: "settings",
      settingsPage: "flags",
    });
  });

  it("keeps links to the retired canvas view working", () => {
    expect(parseViewRoute("?v=canvas").view).toBe("chat");
  });
});

describe("formatViewRoute", () => {
  it("formats a plain view", () => {
    expect(formatViewRoute({ view: "health", settingsPage: "providers" })).toBe(
      "?v=health"
    );
  });

  it("carries the sub-page only for settings", () => {
    expect(formatViewRoute({ view: "settings", settingsPage: "secrets" })).toBe(
      "?v=settings&s=secrets"
    );
  });

  it("round-trips through parse", () => {
    const route = { view: "settings", settingsPage: "plugins" } as const;
    expect(parseViewRoute(formatViewRoute(route))).toEqual(route);
  });

  it("preserves unrelated query parameters and drops a stale sub-page", () => {
    const out = formatViewRoute(
      { view: "runs", settingsPage: "providers" },
      "?v=settings&s=flags&debug=1"
    );
    expect(out).toContain("v=runs");
    expect(out).toContain("debug=1");
    expect(out).not.toContain("s=flags");
  });
});

describe("useViewRoute", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("starts on the default view", () => {
    const { result } = renderHook(() => useViewRoute());
    expect(result.current.view).toBe("chat");
  });

  it("adopts the view named in the URL on mount", () => {
    window.history.replaceState(null, "", "/?v=fleet");
    const { result } = renderHook(() => useViewRoute());
    expect(result.current.view).toBe("fleet");
  });

  it("writes the view into the query string when navigating", () => {
    const { result } = renderHook(() => useViewRoute());
    act(() => result.current.navigate("runs"));
    expect(result.current.view).toBe("runs");
    expect(window.location.search).toBe("?v=runs");
  });

  it("writes the settings sub-page too", () => {
    const { result } = renderHook(() => useViewRoute());
    act(() => result.current.navigate("settings", "channels"));
    expect(result.current.settingsPage).toBe("channels");
    expect(window.location.search).toBe("?v=settings&s=channels");
  });

  it("follows real back/forward navigation", async () => {
    const { result } = renderHook(() => useViewRoute());
    act(() => result.current.navigate("runs"));
    act(() => result.current.navigate("health"));
    expect(window.location.search).toBe("?v=health");

    act(() => {
      window.history.back();
    });
    await waitFor(() => expect(result.current.view).toBe("runs"));

    act(() => {
      window.history.forward();
    });
    await waitFor(() => expect(result.current.view).toBe("health"));
  });

  it("pushes one history entry per navigation under StrictMode", () => {
    const push = vi.spyOn(window.history, "pushState");
    const { result } = renderHook(() => useViewRoute(), { wrapper: StrictMode });
    act(() => result.current.navigate("runs"));
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("does not push a duplicate entry for the route already showing", () => {
    const { result } = renderHook(() => useViewRoute());
    act(() => result.current.navigate("runs"));
    const push = vi.spyOn(window.history, "pushState");
    act(() => result.current.navigate("runs"));
    act(() => result.current.navigate("runs"));
    expect(push).not.toHaveBeenCalled();
    expect(result.current.view).toBe("runs");
  });

  it("normalises an unknown view in the URL instead of leaving a bad link", async () => {
    window.history.replaceState(null, "", "/?v=bogus");
    const { result } = renderHook(() => useViewRoute());
    expect(result.current.view).toBe("chat");
    await waitFor(() => expect(window.location.search).toBe("?v=chat"));
  });

  it("rewrites a retired view link to where that screen lives now", async () => {
    window.history.replaceState(null, "", "/?v=controls");
    const { result } = renderHook(() => useViewRoute());
    expect(result.current.view).toBe("settings");
    await waitFor(() => expect(window.location.search).toBe("?v=settings&s=flags"));
  });

  it("leaves a bare URL alone", () => {
    const { result } = renderHook(() => useViewRoute());
    expect(result.current.view).toBe("chat");
    expect(window.location.search).toBe("");
  });
});
