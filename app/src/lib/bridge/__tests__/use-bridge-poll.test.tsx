/**
 * The shared listing poll, tested where it lives rather than three times over
 * in three views.
 *
 * The four properties here are the ones the three copies each had to get right
 * on their own: poll only while visible, keep the 60 s beat when the caller
 * re-renders, tell a 403 apart from a failure, and answer a refusal in the
 * server's words.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BRIDGE_UNREACHABLE, useBridgePoll } from "../use-bridge-poll";

function Probe({ visible, url = "/api/bridge/api/things" }: { visible: boolean; url?: string }) {
  const { loading, error, forbidden, reload } = useBridgePoll({
    visible,
    url,
    // Deliberately a fresh closure on every render: the hook must hold it in a
    // ref, or the interval below is rebuilt on each tick.
    onData: (body) => {
      const count = (body as { count?: number })?.count ?? 0;
      const node = document.getElementById("count");
      if (node) node.textContent = String(count);
    },
  });
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="error">{error ?? ""}</span>
      <span data-testid="forbidden">{String(forbidden)}</span>
      <span id="count" data-testid="count" />
      <button data-testid="reload" onClick={reload}>
        reload
      </button>
    </div>
  );
}

function mockFetch(answer: (calls: number) => { status: number; body: unknown }) {
  let calls = 0;
  const fetchMock = vi.fn(async () => {
    calls += 1;
    const { status, body } = answer(calls);
    return { ok: status < 400, status, json: async () => body } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useBridgePoll", () => {
  it("does not touch the bridge while the caller is hidden", () => {
    const fetchMock = mockFetch(() => ({ status: 200, body: { count: 1 } }));
    render(<Probe visible={false} />);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reads once on mount and again every 60 seconds", async () => {
    const fetchMock = mockFetch((n) => ({ status: 200, body: { count: n } }));
    render(<Probe visible />);
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"));

    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("stops once the caller is hidden", async () => {
    const fetchMock = mockFetch(() => ({ status: 200, body: { count: 1 } }));
    const { rerender } = render(<Probe visible />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    rerender(<Probe visible={false} />);
    await vi.advanceTimersByTimeAsync(180_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("answers a refusal in the server's own words", async () => {
    mockFetch(() => ({ status: 503, body: { detail: "the account store is unavailable" } }));
    render(<Probe visible />);
    await waitFor(() =>
      expect(screen.getByTestId("error").textContent).toBe("the account store is unavailable")
    );
    expect(screen.getByTestId("forbidden").textContent).toBe("false");
    expect(screen.getByTestId("loading").textContent).toBe("false");
  });

  it("tells a 403 apart from a failure", async () => {
    mockFetch(() => ({ status: 403, body: { detail: "operator only" } }));
    render(<Probe visible />);
    await waitFor(() => expect(screen.getByTestId("forbidden").textContent).toBe("true"));
    expect(screen.getByTestId("error").textContent).toBe("");
  });

  it("has one sentence for a request that never arrived", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      })
    );
    render(<Probe visible />);
    await waitFor(() =>
      expect(screen.getByTestId("error").textContent).toBe(BRIDGE_UNREACHABLE)
    );
  });
});
