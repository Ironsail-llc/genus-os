import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GoalsView } from "@/components/views/goals-view";

const goal = {
  id: "g1", objective: "Deliver report", success_criteria: ["Receipt verified"],
  kind: "long", mode: "finite", status: "waiting", version: 4, parent_goal_id: null,
  checkpoint: "Report sent", next_action: "Verify receipt", blocker: "",
  ready_at: "2030-01-01T00:00:00Z", tokens_used: 42, token_budget: 1000000, cost_usd: 0.02,
  cost_budget_usd: 5, attempts: 3, max_attempts: 50, deadline_at: "2030-02-01T00:00:00Z",
  evidence: [], wait: { reason: "Waiting for reply" }, assessment: null,
  tasks: [{ id: "t1", title: "Send report", status: "DONE" }], runs: [], history: [],
};
const response = (body: unknown, ok = true) => Promise.resolve({ ok, json: async () => body });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("GoalsView", () => {
  it("shows waiting goals, linked tasks and pause controls", async () => {
    const fetch = vi.fn((_path: string, options?: RequestInit) => {
      if (options?.method === "PATCH") return response({ goal: { ...goal, status: "paused", version: 5 } });
      return _path.endsWith("/g1") ? response({ goal }) : response({ goals: [goal], enabled: true });
    });
    vi.stubGlobal("fetch", fetch);
    render(<GoalsView visible />);
    fireEvent.click(await screen.findByRole("button", { name: /Deliver report/ }));
    expect(await screen.findByText("Send report · DONE")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/bridge/api/goals/g1", expect.objectContaining({
      method: "PATCH", body: JSON.stringify({ action: "pause", version: 4 }),
    })));
  });

  it("creates an ongoing goal with explicit criteria", async () => {
    const fetch = vi.fn((_path: string, options?: RequestInit) => options?.method === "POST"
      ? response({ goal }) : response({ goals: [], enabled: false }));
    vi.stubGlobal("fetch", fetch);
    render(<GoalsView visible />);
    fireEvent.change(screen.getByLabelText("Objective"), { target: { value: "Keep reports current" } });
    fireEvent.change(screen.getByLabelText("Success criteria, one per line"), { target: { value: "Updated daily\nReceipt verified" } });
    fireEvent.change(screen.getByLabelText(/Goal type/), { target: { value: "long" } });
    fireEvent.click(screen.getByLabelText("Ongoing target"));
    fireEvent.click(screen.getByRole("button", { name: "Set goal" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/bridge/api/goals", expect.objectContaining({
      method: "POST", body: expect.stringContaining('"mode":"ongoing"'),
    })));
  });

  it("surfaces stale update errors instead of claiming success", async () => {
    vi.stubGlobal("fetch", vi.fn((path: string, options?: RequestInit) => options?.method === "PATCH"
      ? response({ detail: "stale goal version; reload before updating" }, false)
      : path.endsWith("/g1") ? response({ goal }) : response({ goals: [goal], enabled: true })));
    render(<GoalsView visible />);
    fireEvent.click(await screen.findByRole("button", { name: /Deliver report/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("stale goal version");
  });
});


it("refreshes selected goal progress and uses its latest version for controls", async () => {
  let poll: (() => void) | undefined;
  vi.spyOn(globalThis, "setInterval").mockImplementation((callback) => {
    poll = callback as () => void;
    return 1 as unknown as ReturnType<typeof setInterval>;
  });
  let current = goal;
  const fetch = vi.fn((path: string, options?: RequestInit) => options?.method === "PATCH"
    ? response({ goal: current })
    : path.endsWith("/g1") ? response({ goal: current }) : response({ goals: [current], enabled: true }));
  vi.stubGlobal("fetch", fetch);
  render(<GoalsView visible />);
  fireEvent.click(await screen.findByRole("button", { name: /Deliver report/ }));
  await screen.findByRole("button", { name: "Pause" });
  current = { ...goal, version: 7, checkpoint: "Receipt checked by live agent" };
  await act(async () => { poll?.(); });
  expect(await screen.findByText("Receipt checked by live agent")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Pause" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/bridge/api/goals/g1", expect.objectContaining({
    method: "PATCH", body: JSON.stringify({ action: "pause", version: 7 }),
  })));
});
