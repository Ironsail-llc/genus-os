/**
 * The Goals view's refresh beat: every 30 seconds while it is on screen (it
 * was 5 s, two requests a beat), and nothing at all while the browser tab is
 * hidden. No goal event exists on the stream yet to drive it faster.
 */
import { render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GoalsView } from "../goals-view";

function mockGoals() {
  const fetchMock = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ goals: [], enabled: true }),
  }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function hide(hidden: boolean) {
  Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
  Object.defineProperty(document, "visibilityState", {
    value: hidden ? "hidden" : "visible",
    configurable: true,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

function renderGoals(visible = true) {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <GoalsView visible={visible} />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  hide(false);
});

describe("GoalsView — refresh beat", () => {
  it("reads the list once on show, then every 30 seconds, not every 5", async () => {
    const fetchMock = mockGoals();
    renderGoals();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(29_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1_000);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("does not read while hidden in the Helm", async () => {
    const fetchMock = mockGoals();
    renderGoals(false);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not read while the browser tab is hidden", async () => {
    const fetchMock = mockGoals();
    renderGoals();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    hide(true);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
