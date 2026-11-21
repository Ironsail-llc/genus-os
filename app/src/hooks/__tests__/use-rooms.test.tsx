/**
 * useRooms: the caller's rooms from `GET /api/rooms` through the bridge proxy,
 * with unread counts, kept fresh by `room.*` events on the agent stream.
 * useRoomEvents: one room's events, for the room that is open.
 */
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StreamEvent } from "@/lib/event-bus/use-event-stream";
import { AGENT_ID, REPORT_ID, SUMMARIES, reply } from "@/lib/rooms/__tests__/fixtures";

let streamEvents: StreamEvent[] = [];
const streamOptions: Array<Record<string, unknown>> = [];
vi.mock("@/lib/event-bus/use-event-stream", () => ({
  useEventStream: (options: Record<string, unknown>) => {
    streamOptions.push(options);
    return { events: streamEvents, connected: true, synced: true };
  },
}));

import { useRoomEvents, useRooms } from "../use-rooms";

function roomEvent(id: string, type: string, roomId: string): StreamEvent {
  return {
    id,
    stream: "agent",
    timestamp: "2026-11-21T00:00:00Z",
    type,
    source: "engine",
    actor: "main",
    payload: { room_id: roomId, tenant_id: "test-tenant" },
    correlation_id: "",
  };
}

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  streamEvents = [];
  streamOptions.length = 0;
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/bridge/api/rooms" && (!init?.method || init.method === "GET")) return reply({ rooms: SUMMARIES });
    if (url.endsWith("/read")) return reply({ ok: true });
    return reply({ error: "unexpected" }, 500);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function listCalls() {
  return fetchMock.mock.calls.filter(([url, init]) => String(url) === "/api/bridge/api/rooms" && !init?.method).length;
}

describe("useRooms", () => {
  it("lists the caller's rooms through the bridge proxy, with unread counts", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.rooms).toHaveLength(4));
    expect(result.current.rooms[0]).toMatchObject({ id: REPORT_ID, unread: 3 });
    expect(result.current.totalUnread).toBe(3);
    expect(result.current.error).toBeNull();
  });

  it("reports a refused listing instead of showing an empty one", async () => {
    fetchMock.mockImplementation(async () => reply({ error: "nope" }, 403));
    const { result } = renderHook(() => useRooms(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.error).toEqual({ endpoint: "/api/bridge/api/rooms", status: 403 }));
    expect(result.current.rooms).toEqual([]);
  });

  it("reads the list again when a room event arrives, once per event", async () => {
    const Wrapper = wrapper();
    const { result, rerender } = renderHook(() => useRooms({ live: true }), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.rooms).toHaveLength(4));
    expect(listCalls()).toBe(1);
    expect(streamOptions.at(-1)).toMatchObject({ streams: ["agent"], enabled: true });

    streamEvents = [roomEvent("5-0", "room.message", REPORT_ID)];
    rerender();
    await waitFor(() => expect(listCalls()).toBe(2));
    rerender();
    expect(listCalls()).toBe(2);

    // A non-room event changes nothing.
    streamEvents = [{ ...roomEvent("6-0", "task.created", REPORT_ID), payload: {} }, ...streamEvents];
    rerender();
    expect(listCalls()).toBe(2);
  });

  it("does not subscribe to the stream unless asked to be live", () => {
    renderHook(() => useRooms(), { wrapper: wrapper() });
    expect(streamOptions.at(-1)).toMatchObject({ enabled: false });
  });

  it("marks a room read: one POST, and its badge clears at once", async () => {
    const { result } = renderHook(() => useRooms(), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.rooms).toHaveLength(4));
    await act(async () => {
      await result.current.markRead(REPORT_ID);
    });
    expect(fetchMock).toHaveBeenCalledWith(`/api/bridge/api/rooms/${REPORT_ID}/read`, expect.objectContaining({ method: "POST" }));
    expect(result.current.rooms.find((r) => r.id === REPORT_ID)?.unread).toBe(0);
    expect(result.current.totalUnread).toBe(0);
  });
});

describe("useRoomEvents", () => {
  function Probe({ roomId, seen }: { roomId: string; seen: string[] }) {
    useRoomEvents(roomId, (event) => seen.push(`${event.type}:${event.id}`));
    return <span data-testid="probe" />;
  }

  it("hands over only the open room's room.* events, each once", () => {
    const seen: string[] = [];
    const { rerender } = render(<Probe roomId={REPORT_ID} seen={seen} />);
    streamEvents = [
      roomEvent("3-0", "room.edition", AGENT_ID),
      roomEvent("2-0", "room.message", REPORT_ID),
      { ...roomEvent("1-0", "task.updated", REPORT_ID) },
    ];
    rerender(<Probe roomId={REPORT_ID} seen={seen} />);
    streamEvents = [roomEvent("4-0", "room.edition", REPORT_ID), ...streamEvents];
    rerender(<Probe roomId={REPORT_ID} seen={seen} />);
    rerender(<Probe roomId={REPORT_ID} seen={seen} />);
    expect(screen.getByTestId("probe")).toBeInTheDocument();
    expect(seen).toEqual(["room.message:2-0", "room.edition:4-0"]);
  });
});
