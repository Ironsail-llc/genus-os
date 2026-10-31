/**
 * useAgents through the shared data layer: one request for every reader under
 * one client, one 60 s beat, no beat while the tab is hidden, and no request
 * left running for a reader that has gone.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAgents } from "../use-agents";

function Reader({ id }: { id: string }) {
  const { summary, isLoading } = useAgents();
  return (
    <span data-testid={id}>
      {isLoading ? "loading" : `${summary.healthy}/${summary.total}`}
    </span>
  );
}

function withClient(children: ReactNode) {
  const client = new QueryClient();
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const signals: AbortSignal[] = [];

function mockAgents(pending = false) {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.signal) signals.push(init.signal);
    if (pending) return new Promise<Response>(() => {});
    return {
      ok: true,
      status: 200,
      json: async () => ({
        data: {
          agents: [
            { name: "worker", status: "healthy" },
            { name: "broken", status: "failed" },
          ],
        },
      }),
    } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Stand in for the browser hiding or showing the tab. */
function hide(hidden: boolean) {
  Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
  Object.defineProperty(document, "visibilityState", {
    value: hidden ? "hidden" : "visible",
    configurable: true,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  signals.length = 0;
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  hide(false);
});

describe("useAgents — shared data layer", () => {
  it("sends one agent_status request for three readers under one client", async () => {
    const fetchMock = mockAgents();
    render(withClient(<><Reader id="a" /><Reader id="b" /><Reader id="c" /></>));
    await waitFor(() => expect(screen.getByTestId("c").textContent).toBe("1/2"));
    expect(screen.getByTestId("a").textContent).toBe("1/2");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("polls once every 60 seconds for all readers together", async () => {
    const fetchMock = mockAgents();
    render(withClient(<><Reader id="a" /><Reader id="b" /></>));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("does not poll while the tab is hidden", async () => {
    const fetchMock = mockAgents();
    render(withClient(<Reader id="a" />));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    hide(true);
    await vi.advanceTimersByTimeAsync(300_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    hide(false);
    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
  });

  it("aborts the request in flight when the last reader unmounts", async () => {
    mockAgents(true);
    const { unmount } = render(withClient(<Reader id="a" />));
    await waitFor(() => expect(signals).toHaveLength(1));
    expect(signals[0].aborted).toBe(false);
    unmount();
    await waitFor(() => expect(signals[0].aborted).toBe(true));
  });
});
