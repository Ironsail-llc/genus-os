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
