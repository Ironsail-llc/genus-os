import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatRecovery } from "../chat-recovery";

const request = { requestId: "original-request", agent: "scheduler" };
const answer = (record: unknown) => ({ ok: true, json: async () => record });

async function advance(ms = 0) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

describe("audit recovery under interruption", () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("recovers after unavailable, absent and active records without executing work", async () => {
    vi.useFakeTimers();
    const recovered = vi.fn();
    const fetch = vi.fn()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(answer({ terminal: false, state: "not_found" }))
      .mockResolvedValueOnce(answer({ terminal: false, state: "running" }))
      .mockResolvedValueOnce(answer({ terminal: true, state: "completed", text: "Stored receipt confirms completion" }));
    vi.stubGlobal("fetch", fetch);
    render(<ChatRecovery request={request} messageId="message" onRecovered={recovered} />);
    await advance();
    expect(screen.getByText(/Reconnecting to read/)).toBeTruthy();
    await advance(1000);
    expect(screen.getByText(/Checking the original request/)).toBeTruthy();
    await advance(2000);
    expect(screen.getByText(/original run is still working/)).toBeTruthy();
    expect(recovered).not.toHaveBeenCalled();
    await advance(4000);
    expect(recovered).toHaveBeenCalledExactlyOnceWith("message", "Stored receipt confirms completion");
    await advance(60_000);
    expect(fetch).toHaveBeenCalledTimes(4);
    for (const [url, options] of fetch.mock.calls) {
      const query = new URL(url, "http://test");
      expect(query.pathname).toBe("/api/chat/outcome");
      expect(query.searchParams.get("request_id")).toBe(request.requestId);
      expect(query.searchParams.get("agent")).toBe(request.agent);
      expect(options.method ?? "GET").toBe("GET");
      expect(options.body).toBeUndefined();
      expect(options.cache).toBe("no-store");
    }
  });

  it("backs off prolonged outages and stops polling when the conversation closes", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 502 });
    vi.stubGlobal("fetch", fetch);
    const { unmount } = render(<ChatRecovery request={request} messageId="message" onRecovered={vi.fn()} />);
    await advance();
    for (const delay of [1000, 2000, 4000, 8000, 10_000, 10_000]) await advance(delay);
    expect(fetch).toHaveBeenCalledTimes(7);
    unmount();
    await advance(60_000);
    expect(fetch).toHaveBeenCalledTimes(7);
  });

  it("ignores a late response after unmount even if the transport ignores cancellation", async () => {
    vi.useFakeTimers();
    let resolve!: (value: unknown) => void;
    const fetch = vi.fn().mockImplementation(() => new Promise((done) => { resolve = done; }));
    vi.stubGlobal("fetch", fetch);
    const recovered = vi.fn();
    const { unmount } = render(<ChatRecovery request={request} messageId="message" onRecovered={recovered} />);
    const signal = fetch.mock.calls[0][1].signal as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
    resolve(answer({ terminal: true, text: "Old conversation's result" }));
    await advance(60_000);
    expect(recovered).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("keeps reading pending action evidence after the run ends", async () => {
    vi.useFakeTimers();
    const recovered = vi.fn();
    const fetch = vi.fn()
      .mockResolvedValueOnce(answer({ terminal: true, state: "cancelled", reconciliation_pending: true, text: "Run stopped. Calendar verification is pending." }))
      .mockResolvedValueOnce(answer({ terminal: true, state: "cancelled", reconciliation_pending: false, text: "Run stopped. Recorded calendar change is verified." }));
    vi.stubGlobal("fetch", fetch);
    render(<ChatRecovery request={request} messageId="message" onRecovered={recovered} />);
    await advance();
    expect(recovered).not.toHaveBeenCalled();
    expect(screen.getByText(/Calendar verification is pending/)).toBeTruthy();
    await advance(1000);
    expect(recovered).toHaveBeenCalledExactlyOnceWith("message", "Run stopped. Recorded calendar change is verified.");
    await advance(60_000);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[0][0]).toBe(fetch.mock.calls[1][0]);
  });

  it("shows durable approval while waiting for its original execution record", async () => {
    vi.useFakeTimers();
    const recovered = vi.fn();
    const fetch = vi.fn()
      .mockResolvedValueOnce(answer({ terminal: false, state: "accepted", source: "approval_record" }))
      .mockResolvedValueOnce(answer({ terminal: true, state: "completed", text: "Recorded execution completed." }));
    vi.stubGlobal("fetch", fetch);
    render(<ChatRecovery request={request} messageId="message" onRecovered={recovered} />);
    await advance();
    expect(screen.getByText(/Your approval is recorded/)).toBeTruthy();
    expect(recovered).not.toHaveBeenCalled();
    await advance(1000);
    expect(recovered).toHaveBeenCalledExactlyOnceWith("message", "Recorded execution completed.");
    expect(fetch).toHaveBeenCalledTimes(2);
    for (const [url, options] of fetch.mock.calls) {
      expect(new URL(url, "http://test").searchParams.get("request_id")).toBe(request.requestId);
      expect(options.method ?? "GET").toBe("GET");
    }
  });

});
