/**
 * A room is a `room` parameter on the chat view (`/?v=chat&room={id}`), the
 * link every room tool answers with. No parameter is the main conversation,
 * and the route then reads exactly as it always has.
 */
import { afterEach, describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { formatViewRoute, parseViewRoute, useViewRoute } from "../use-view-route";

const ROOM = "11111111-1111-4111-8111-111111111111";

describe("the room parameter", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("reads a room on the chat view", () => {
    expect(parseViewRoute(`?v=chat&room=${ROOM}`)).toEqual({ view: "chat", settingsPage: "providers", room: ROOM });
  });

  it("reads a bare room link as the chat view", () => {
    expect(parseViewRoute(`?room=${ROOM}`).room).toBe(ROOM);
  });

  it("adds no room key when there is none, so the main route is unchanged", () => {
    expect(parseViewRoute("?v=chat")).toEqual({ view: "chat", settingsPage: "providers" });
    expect("room" in parseViewRoute("?v=chat")).toBe(false);
  });

  it("ignores a room on any other view, and a value that is not a room id", () => {
    expect(parseViewRoute(`?v=tasks&room=${ROOM}`).room).toBeUndefined();
    expect(parseViewRoute("?v=chat&room=agent:main:primary").room).toBeUndefined();
  });

  it("writes the room back, and drops it when leaving the room", () => {
    expect(formatViewRoute({ view: "chat", settingsPage: "providers", room: ROOM })).toBe(`?v=chat&room=${ROOM}`);
    expect(formatViewRoute({ view: "chat", settingsPage: "providers" }, `?v=chat&room=${ROOM}`)).toBe("?v=chat");
    expect(formatViewRoute({ view: "tasks", settingsPage: "providers" }, `?v=chat&room=${ROOM}`)).toBe("?v=tasks");
  });

  it("opens and leaves a room through the hook, with history entries", () => {
    window.history.replaceState(null, "", "/?v=chat");
    const { result } = renderHook(() => useViewRoute());
    act(() => result.current.openRoom(ROOM));
    expect(window.location.search).toBe(`?v=chat&room=${ROOM}`);
    expect(result.current.room).toBe(ROOM);
    expect(result.current.view).toBe("chat");

    act(() => result.current.openRoom(null));
    expect(window.location.search).toBe("?v=chat");
    expect(result.current.room).toBeUndefined();
  });

  it("drops the room when the operator navigates to another view", () => {
    window.history.replaceState(null, "", `/?v=chat&room=${ROOM}`);
    const { result } = renderHook(() => useViewRoute());
    expect(result.current.room).toBe(ROOM);
    act(() => result.current.navigate("tasks"));
    expect(window.location.search).toBe("?v=tasks");
    expect(result.current.room).toBeUndefined();
  });

  it("strips a malformed room from the address bar on load", () => {
    window.history.replaceState(null, "", "/?v=chat&room=not-a-room");
    renderHook(() => useViewRoute());
    expect(window.location.search).toBe("?v=chat");
  });
});
