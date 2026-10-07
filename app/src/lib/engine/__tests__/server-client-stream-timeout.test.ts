import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * A streamed turn may run for many minutes. The engine gets a bounded time to
 * ANSWER (send response headers), but once it has, the stream must not be cut
 * — an AbortSignal.timeout on the whole fetch aborted every turn longer than
 * 120 s mid-reply, which the chat then showed as "Connection interrupted".
 */

vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: async () => ({ Authorization: "Bearer test-token" }),
}));

type Call = { signal: AbortSignal };

describe("EngineClient — streamed calls are bounded on connect, not on duration", () => {
  let calls: Call[];

  beforeEach(() => {
    vi.useFakeTimers();
    calls = [];
    // AbortSignal.timeout runs on a native timer the fake clock cannot move;
    // route it through setTimeout so a whole-request timeout is observable.
    vi.spyOn(AbortSignal, "timeout").mockImplementation((ms: number) => {
      const controller = new AbortController();
      setTimeout(() => controller.abort(new DOMException("timed out", "TimeoutError")), ms);
      return controller.signal;
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const streamed = [
    ["chatSend", (c: Record<string, (...a: unknown[]) => Promise<unknown>>) => c.chatSend("hello")],
    ["planStart", (c: Record<string, (...a: unknown[]) => Promise<unknown>>) => c.planStart("hello")],
    ["planApprove", (c: Record<string, (...a: unknown[]) => Promise<unknown>>) => c.planApprove("plan-1")],
    ["deepStart", (c: Record<string, (...a: unknown[]) => Promise<unknown>>) => c.deepStart("hello")],
  ] as const;

  it.each(streamed)("%s keeps the stream open past two minutes once the engine answered", async (_name, call) => {
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      calls.push({ signal: init.signal as AbortSignal });
      return { ok: true, status: 200, statusText: "OK", body: null };
    }));
    const { getEngineClient } = await import("../server-client");
    await call(getEngineClient() as unknown as Record<string, (...a: unknown[]) => Promise<unknown>>);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(calls[0].signal.aborted).toBe(false);
  });

  it.each(streamed)("%s still gives up when the engine never answers", async (_name, call) => {
    vi.stubGlobal("fetch", vi.fn((_url: string, init: RequestInit) => {
      calls.push({ signal: init.signal as AbortSignal });
      return new Promise((_resolve, reject) => {
        (init.signal as AbortSignal).addEventListener("abort", () => reject((init.signal as AbortSignal).reason));
      });
    }));
    const { getEngineClient } = await import("../server-client");
    const pending = call(getEngineClient() as unknown as Record<string, (...a: unknown[]) => Promise<unknown>>);
    const settled = expect(pending).rejects.toBeDefined();
    await vi.advanceTimersByTimeAsync(121_000);
    await settled;
    expect(calls[0].signal.aborted).toBe(true);
  });
});
