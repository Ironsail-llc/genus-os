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

function Probe({
  visible,
  url = "/api/bridge/api/things",
  paused,
}: {
  visible: boolean;
  url?: string;
  paused?: boolean;
}) {
  const { loading, error, forbidden, status, reload } = useBridgePoll({
    visible,
    url,
    paused,
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
      <span data-testid="status">{String(status)}</span>
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

/** jsdom's `document.hidden` is a read-only getter; stand one in and announce it. */
function hide(hidden: boolean) {
  Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Object.defineProperty(document, "hidden", { value: false, configurable: true });
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

  /**
   * `paused` is for a listing whose refresh is the operator's choice — the
   * Logs pane, where auto-refresh is off by default because ten seconds of
   * journald is a real read on the box and most of the time the operator is
   * staring at one window, not watching a stream.
   *
   * It stops the BEAT, not the reader: the listing still loads when it comes
   * on screen and still reloads when the URL changes, or a paused pane would
   * be a blank pane.
   */
  it("still reads once while paused, and never ticks", async () => {
    const fetchMock = mockFetch((n) => ({ status: 200, body: { count: n } }));
    render(<Probe visible paused />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(600_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("starts ticking when the pause is lifted, and stops again when it returns", async () => {
    const fetchMock = mockFetch((n) => ({ status: 200, body: { count: n } }));
    const { rerender } = render(<Probe visible paused />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    rerender(<Probe visible paused={false} />);
    await vi.advanceTimersByTimeAsync(60_000);
    const whileRunning = fetchMock.mock.calls.length;
    expect(whileRunning).toBeGreaterThan(1);

    rerender(<Probe visible paused />);
    const whenRepaused = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(600_000);
    expect(fetchMock).toHaveBeenCalledTimes(whenRepaused);
  });

  /**
   * `status` exists so a caller can decide WHERE a refusal goes: a 422 names a
   * query parameter the operator can fix and belongs beside that input, while
   * a 500 belongs in a banner. It has to clear on the next success, or a page
   * would keep a field marked red over data that loaded fine.
   */
  it("reports the kind of refusal, and clears it on the next success", async () => {
    let fail = true;
    mockFetch(() =>
      fail
        ? { status: 422, body: { detail: "since must be an ISO-8601 date" } }
        : { status: 200, body: { count: 1 } }
    );
    render(<Probe visible />);
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("422"));

    fail = false;
    screen.getByTestId("reload").click();
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("null"));
    expect(screen.getByTestId("error").textContent).toBe("");
  });

  it("says nothing about a status when the request never arrived", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      })
    );
    render(<Probe visible />);
    await waitFor(() => expect(screen.getByTestId("error").textContent).toBe(BRIDGE_UNREACHABLE));
    // There was no reply, so there is no status to report — not a stale one.
    expect(screen.getByTestId("status").textContent).toBe("null");
  });

  /**
   * `visible` is "this view is the one on screen in the Helm". It says nothing
   * about whether the BROWSER TAB is on screen, and Logs is the first caller
   * where each beat spawns a `journalctl` on the box — so a Helm left open
   * behind another window was spawning one every ten seconds, for nobody.
   */
  it("stops beating while the browser tab is hidden, and resumes when it is back", async () => {
    const fetchMock = mockFetch((n) => ({ status: 200, body: { count: n } }));
    render(<Probe visible />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    hide(true);
    await vi.advanceTimersByTimeAsync(300_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    hide(false);
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
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
