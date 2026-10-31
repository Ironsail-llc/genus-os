/**
 * The Goals view's refresh beat: every 30 seconds while it is on screen (it
 * was 5 s, two requests a beat), and nothing at all while the browser tab is
 * hidden. No goal event exists on the stream yet to drive it faster.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

describe("GoalsView — after a write", () => {
  it("shows a newly created goal in the list straight away, not on the next beat", async () => {
    const created = {
      id: "g2", objective: "Ship the report", kind: "short", mode: "finite", status: "queued",
      version: 1, success_criteria: ["Sent"], parent_goal_id: null, checkpoint: "", next_action: "",
      blocker: "", ready_at: "", tokens_used: 0, token_budget: null, cost_usd: 0,
      cost_budget_usd: null, attempts: 0, max_attempts: null, deadline_at: null, evidence: [],
      wait: null, assessment: null,
    };
    let goals: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        goals = [created];
        return { ok: true, status: 200, json: async () => ({ goal: created }) } as Response;
      }
      if (path.endsWith("/g2")) return { ok: true, status: 200, json: async () => ({ goal: created }) } as Response;
      return { ok: true, status: 200, json: async () => ({ goals, enabled: true }) } as Response;
    }));
    renderGoals();
    await waitFor(() => expect(screen.getByRole("button", { name: "Set goal" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Objective"), { target: { value: "Ship the report" } });
    fireEvent.change(screen.getByLabelText("Success criteria, one per line"), { target: { value: "Sent" } });
    fireEvent.click(screen.getByRole("button", { name: "Set goal" }));

    // Well inside one 30 s beat.
    await vi.advanceTimersByTimeAsync(2_000);
    expect(screen.getByRole("button", { name: /Ship the report/ })).toBeInTheDocument();
  });
});
