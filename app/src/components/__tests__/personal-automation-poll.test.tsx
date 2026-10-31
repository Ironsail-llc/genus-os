/**
 * The personal-automation panel's refresh beat: one refresh (status and
 * operations) every 30 seconds (it was 5 s), and none while the tab is hidden.
 * No autonomy event exists on the stream yet to drive it faster.
 */
import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PersonalAutomationPanel } from "../personal-automation-panel";

function mockAutonomy() {
  const fetchMock = vi.fn(async (url: string) => ({
    ok: true,
    status: 200,
    json: async () =>
      url.endsWith("/status")
        ? {
            resources: [],
            grants: [],
            settings: { enabled: true, managed_browser: false, payment_processing: false },
          }
        : { operations: [] },
  }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function statusCalls(fetchMock: ReturnType<typeof mockAutonomy>) {
  return fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/status")).length;
}

function hide(hidden: boolean) {
  Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
  Object.defineProperty(document, "visibilityState", {
    value: hidden ? "hidden" : "visible",
    configurable: true,
  });
  document.dispatchEvent(new Event("visibilitychange"));
  window.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  hide(false);
});

describe("PersonalAutomationPanel — refresh beat", () => {
  it("refreshes every 30 seconds, not every 5", async () => {
    const fetchMock = mockAutonomy();
    render(<PersonalAutomationPanel />);
    await waitFor(() => expect(statusCalls(fetchMock)).toBe(1));

    await vi.advanceTimersByTimeAsync(29_000);
    expect(statusCalls(fetchMock)).toBe(1);

    await vi.advanceTimersByTimeAsync(1_000);
    await waitFor(() => expect(statusCalls(fetchMock)).toBe(2));
  });

  it("does not refresh while the browser tab is hidden", async () => {
    const fetchMock = mockAutonomy();
    render(<PersonalAutomationPanel />);
    await waitFor(() => expect(statusCalls(fetchMock)).toBe(1));

    hide(true);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(statusCalls(fetchMock)).toBe(1);
  });
});
