/**
 * A Logs search is a `q` parameter on the logs view (`/?v=logs&q=...`): the
 * link the run drawer's "Logs for this run" opens. Only the logs view reads
 * it, and leaving the view drops it.
 */
import { afterEach, describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { formatViewRoute, logsLink, parseViewRoute, useViewRoute } from "../use-view-route";

const QUERY = '"run_id=r1"';

describe("the logs query parameter", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("reads a query on the logs view", () => {
    expect(parseViewRoute(`?v=logs&q=${encodeURIComponent(QUERY)}`)).toEqual({
      view: "logs",
      settingsPage: "providers",
      query: QUERY,
    });
  });

  it("adds no query key when there is none, or on another view", () => {
    expect("query" in parseViewRoute("?v=logs")).toBe(false);
    expect("query" in parseViewRoute("?v=logs&q=")).toBe(false);
    expect(parseViewRoute(`?v=runs&q=${encodeURIComponent(QUERY)}`).query).toBeUndefined();
  });

  it("writes the query back, and drops it when leaving the view", () => {
    expect(formatViewRoute({ view: "logs", settingsPage: "providers", query: QUERY })).toBe(
      `?v=logs&q=${encodeURIComponent(QUERY)}`,
    );
    expect(formatViewRoute({ view: "runs", settingsPage: "providers" }, "?v=logs&q=x")).toBe("?v=runs");
  });

  it("builds the link a page can put in an href", () => {
    expect(logsLink(QUERY)).toBe(`?v=logs&q=${encodeURIComponent(QUERY)}`);
  });

  it("carries the window the search should open on", () => {
    const since = "2099-01-01T08:55:00.000Z";
    const search = `?v=logs&q=${encodeURIComponent(QUERY)}&since=${encodeURIComponent(since)}`;

    expect(parseViewRoute(search).since).toBe(since);
    expect(parseViewRoute(`?v=logs&q=x&since=7d`).since).toBe("7d");
    expect(logsLink(QUERY, since)).toBe(search);
    expect(formatViewRoute({ view: "runs", settingsPage: "providers" }, search)).toBe("?v=runs");
  });

  it("drops a window the bridge would refuse, or one without a search", () => {
    expect("since" in parseViewRoute("?v=logs&q=x&since=yesterday")).toBe(false);
    expect("since" in parseViewRoute("?v=logs&since=7d")).toBe(false);
  });

  it("opens the logs view on a query and window through the hook", () => {
    const { result } = renderHook(() => useViewRoute());

    act(() => result.current.openLogs(QUERY, "7d"));

    expect(result.current.query).toBe(QUERY);
    expect(result.current.since).toBe("7d");
    expect(new URLSearchParams(window.location.search).get("since")).toBe("7d");
  });

  it("opens the logs view on a query through the hook, with a history entry", () => {
    const { result } = renderHook(() => useViewRoute());

    act(() => result.current.openLogs(QUERY));

    expect(result.current.view).toBe("logs");
    expect(result.current.query).toBe(QUERY);
    expect(new URLSearchParams(window.location.search).get("q")).toBe(QUERY);

    act(() => result.current.navigate("runs"));
    expect(result.current.query).toBeUndefined();
    expect(window.location.search).toBe("?v=runs");
  });
});
