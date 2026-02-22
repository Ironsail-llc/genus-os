import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useEventStream } from "../use-event-stream";

// Mock EventSource with construction tracking
let constructedUrls: string[] = [];

class MockEventSource {
  url: string;
  listeners: Record<string, ((e: any) => void)[]> = {};
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  static lastInstance: MockEventSource | null = null;

  constructor(url: string) {
    this.url = url;
    constructedUrls.push(url);
    MockEventSource.lastInstance = this;
    // Auto-open after a tick
    setTimeout(() => {
      if (this.onopen) this.onopen();
    }, 0);
  }

  addEventListener(type: string, handler: (e: any) => void) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(handler);
  }

  close() {
    this.closed = true;
  }

  // Test helper: emit an event
  _emit(type: string, data: any) {
    for (const handler of this.listeners[type] || []) {
      handler({ data: typeof data === "string" ? data : JSON.stringify(data) });
    }
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  constructedUrls = [];
  MockEventSource.lastInstance = null;
  // @ts-ignore
  global.EventSource = MockEventSource;
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useEventStream", () => {
  it("connects to SSE endpoint with correct streams", async () => {
    renderHook(() => useEventStream({ streams: ["email", "crm"] }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(constructedUrls).toContain("/api/events/stream?streams=email,crm");
  });

  it("receives and stores events", async () => {
    const { result } = renderHook(() => useEventStream());

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });

    act(() => {
      MockEventSource.lastInstance?._emit("message", {
        id: "1-0",
        stream: "email",
        timestamp: "2026-01-01T00:00:00Z",
        type: "email.new",
        source: "email_sync",
        actor: "robothor",
        payload: { subject: "Test" },
        correlation_id: "",
      });
    });

    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0].type).toBe("email.new");
  });

  it("limits events to maxEvents", async () => {
    const { result } = renderHook(() =>
      useEventStream({ maxEvents: 3 })
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });

    act(() => {
      for (let i = 0; i < 5; i++) {
        MockEventSource.lastInstance?._emit("message", {
          id: `${i}-0`,
          stream: "email",
          timestamp: "t",
          type: "email.new",
          source: "s",
          actor: "a",
          payload: { i },
          correlation_id: "",
        });
      }
    });

    expect(result.current.events).toHaveLength(3);
  });

  it("filters events by stream", async () => {
    const { result } = renderHook(() => useEventStream());

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });

    act(() => {
      MockEventSource.lastInstance?._emit("message", {
        id: "1-0", stream: "email", timestamp: "t",
        type: "email.new", source: "s", actor: "a", payload: {}, correlation_id: "",
      });
      MockEventSource.lastInstance?._emit("message", {
        id: "2-0", stream: "crm", timestamp: "t",
        type: "crm.create", source: "s", actor: "a", payload: {}, correlation_id: "",
      });
    });

    expect(result.current.eventsByStream("email")).toHaveLength(1);
    expect(result.current.eventsByStream("crm")).toHaveLength(1);
  });

  it("does not connect when disabled", async () => {
    renderHook(() => useEventStream({ enabled: false }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(constructedUrls).toHaveLength(0);
  });

  it("clears events", async () => {
    const { result } = renderHook(() => useEventStream());

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });

    act(() => {
      MockEventSource.lastInstance?._emit("message", {
        id: "1-0", stream: "email", timestamp: "t",
        type: "email.new", source: "s", actor: "a", payload: {}, correlation_id: "",
      });
    });

    expect(result.current.events).toHaveLength(1);

    act(() => {
      result.current.clearEvents();
    });

    expect(result.current.events).toHaveLength(0);
  });
});
