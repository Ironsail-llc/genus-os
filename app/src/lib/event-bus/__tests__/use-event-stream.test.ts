import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useEventStream } from "../use-event-stream";

// Mock EventSource with construction tracking
let constructedUrls: string[] = [];

interface MockMessageEvent {
  data: string;
}

class MockEventSource {
  url: string;
  listeners: Record<string, ((e: MockMessageEvent) => void)[]> = {};
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  static lastInstance: MockEventSource | null = null;
  static instances: MockEventSource[] = [];
  static autoOpen = true;

  constructor(url: string) {
    this.url = url;
    constructedUrls.push(url);
    MockEventSource.lastInstance = this;
    MockEventSource.instances.push(this);
    // Auto-open after a tick
    if (MockEventSource.autoOpen) {
      setTimeout(() => {
        if (this.onopen) this.onopen();
      }, 0);
    }
  }

  addEventListener(type: string, handler: (e: MockMessageEvent) => void) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(handler);
  }

  close() {
    this.closed = true;
  }

  // Test helper: emit an event
  _emit(type: string, data: string | Record<string, unknown>) {
    for (const handler of this.listeners[type] || []) {
      handler({ data: typeof data === "string" ? data : JSON.stringify(data) });
    }
  }

  // Test helper: the connection drops
  _fail() {
    if (this.onerror) this.onerror();
  }
}

const openInstances = () => MockEventSource.instances.filter((es) => !es.closed);

function agentEvent(id: string, type = "task.created") {
  return {
    id, stream: "agent", timestamp: "t", type, source: "s", actor: "a",
    payload: {}, correlation_id: "",
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  constructedUrls = [];
  MockEventSource.lastInstance = null;
  MockEventSource.instances = [];
  MockEventSource.autoOpen = true;
  // @ts-expect-error — Mock EventSource for testing
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

  it("does not reconnect when re-rendered with a fresh streams array", async () => {
    // Callers pass `streams: ["agent"]` as a literal: a new array every render.
    const { rerender } = renderHook(() => useEventStream({ streams: ["agent"], maxEvents: 20 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    for (let i = 0; i < 10; i++) rerender();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(constructedUrls).toHaveLength(1);
    expect(openInstances()).toHaveLength(1);
  });

  it("does not reconnect when the same streams arrive in a different order", async () => {
    let streams = ["email", "crm"];
    const { rerender } = renderHook(() => useEventStream({ streams }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    streams = ["crm", "email"];
    rerender();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(constructedUrls).toHaveLength(1);
  });

  it("shares one EventSource between subscribers in the same tab", async () => {
    const a = renderHook(() => useEventStream({ streams: ["agent"] }));
    const b = renderHook(() => useEventStream({ streams: ["agent"] }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(openInstances()).toHaveLength(1);

    act(() => {
      openInstances()[0]._emit("message", agentEvent("7-0"));
    });
    expect(a.result.current.events.map((e) => e.id)).toEqual(["7-0"]);
    expect(b.result.current.events.map((e) => e.id)).toEqual(["7-0"]);
  });

  it("gives each subscriber only the streams it asked for", async () => {
    const agent = renderHook(() => useEventStream({ streams: ["agent"] }));
    const email = renderHook(() => useEventStream({ streams: ["email"] }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(openInstances()).toHaveLength(1);
    expect(openInstances()[0].url).toMatch(/streams=(agent,email|email,agent)$/);

    act(() => {
      const es = openInstances()[0];
      es._emit("message", agentEvent("1-0"));
      es._emit("message", { ...agentEvent("2-0", "email.new"), stream: "email" });
    });
    expect(agent.result.current.events.map((e) => e.id)).toEqual(["1-0"]);
    expect(email.result.current.events.map((e) => e.id)).toEqual(["2-0"]);
  });

  it("delivers an event replayed after a reconnect only once", async () => {
    const { result } = renderHook(() => useEventStream({ streams: ["agent"] }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    act(() => {
      openInstances()[0]._emit("message", agentEvent("3-0"));
      openInstances()[0]._fail();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    act(() => {
      // The server replays its recent events on every new connection.
      openInstances()[0]._emit("message", agentEvent("3-0"));
      openInstances()[0]._emit("message", agentEvent("4-0"));
    });
    expect(result.current.events.map((e) => e.id)).toEqual(["4-0", "3-0"]);
  });

  it("closes the shared EventSource when the last subscriber unmounts", async () => {
    const a = renderHook(() => useEventStream({ streams: ["agent"] }));
    const b = renderHook(() => useEventStream({ streams: ["agent"] }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    a.unmount();
    expect(openInstances()).toHaveLength(1);
    b.unmount();
    expect(openInstances()).toHaveLength(0);
  });

  it("backs off exponentially on errors and resets after a successful open", async () => {
    MockEventSource.autoOpen = false;
    renderHook(() => useEventStream({ streams: ["agent"] }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(constructedUrls).toHaveLength(1);

    const expectReconnectAfter = async (ms: number) => {
      const before = constructedUrls.length;
      act(() => {
        MockEventSource.lastInstance?._fail();
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms - 1);
      });
      expect(constructedUrls).toHaveLength(before);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(constructedUrls).toHaveLength(before + 1);
    };

    await expectReconnectAfter(1000);
    await expectReconnectAfter(2000);
    await expectReconnectAfter(4000);

    // A successful open resets the delay.
    act(() => {
      MockEventSource.lastInstance?.onopen?.();
    });
    await expectReconnectAfter(1000);
  });

  it("caps the reconnect delay at 30 seconds", async () => {
    MockEventSource.autoOpen = false;
    renderHook(() => useEventStream({ streams: ["agent"] }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    for (let i = 0; i < 8; i++) {
      act(() => {
        MockEventSource.lastInstance?._fail();
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
    }
    expect(constructedUrls).toHaveLength(9);
  });
});
