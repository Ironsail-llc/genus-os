import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GoalsView } from "@/components/views/goals-view";

const goal = {
  id: "g1", objective: "Deliver report", success_criteria: ["Receipt verified"],
  kind: "long", mode: "finite", status: "waiting", version: 4, parent_goal_id: null,
  checkpoint: "Report sent", next_action: "Verify receipt", blocker: "",
  ready_at: "2030-01-01T00:00:00Z", tokens_used: 42, token_budget: null, cost_usd: 0.02,
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

it("submits an explicit revision with a reason and current version", async () => {
  const fetch = vi.fn((path: string, options?: RequestInit) => options?.method === "PATCH"
    ? response({ goal }) : path.endsWith("/g1") ? response({ goal }) : response({ goals: [goal], enabled: true }));
  vi.stubGlobal("fetch", fetch);
  render(<GoalsView visible />);
  fireEvent.click(await screen.findByRole("button", { name: /Deliver report/ }));
  fireEvent.change(await screen.findByLabelText("Revised objective"), { target: { value: "Deliver revised report" } });
  fireEvent.change(screen.getByLabelText("Replacement criteria"), { target: { value: "New receipt checked" } });
  fireEvent.change(screen.getByLabelText("Reason for revision"), { target: { value: "Operator changed the destination" } });
  fireEvent.submit(screen.getByLabelText("Reason for revision").closest("form")!);
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/bridge/api/goals/g1", expect.objectContaining({
    method: "PATCH", body: JSON.stringify({ action: "revise", version: 4, note: "Operator changed the destination", objective: "Deliver revised report", success_criteria: ["New receipt checked"] }),
  })));
});

it("creates reviewed child work only after explicit authorization", async () => {
  const fetch = vi.fn((path: string, options?: RequestInit) => options?.method === "POST"
    ? response({ goal }) : path.endsWith("/g1") ? response({ goal }) : response({ goals: [goal], enabled: true }));
  vi.stubGlobal("fetch", fetch);
  render(<GoalsView visible />);
  fireEvent.click(await screen.findByRole("button", { name: /Deliver report/ }));
  fireEvent.change(await screen.findByLabelText("Milestone objective"), { target: { value: "Check delivery" } });
  fireEvent.change(screen.getByLabelText("Milestone criteria"), { target: { value: "Recipient receipt recorded" } });
  expect(fetch.mock.calls.filter(([, opts]) => opts?.method === "POST")).toHaveLength(0);
  fireEvent.submit(screen.getByLabelText("Milestone criteria").closest("form")!);
  await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/bridge/api/goals", expect.objectContaining({
    method: "POST", body: expect.stringContaining('"parent_goal_id":"g1","kind":"short","human_review":true'),
  })));
});


it("keeps approval unavailable until action readback settles, even without a goal version change", async () => {
  let poll: (() => void) | undefined;
  vi.spyOn(globalThis, "setInterval").mockImplementation(callback => {
    poll = callback as () => void;
    return 1 as unknown as ReturnType<typeof setInterval>;
  });
  let current = { ...goal, status: "review", action_evidence: { pending: 1, confirmed: 0 } };
  const fetch = vi.fn((path: string, options?: RequestInit) => {
    if (options?.method && options.method !== "GET") throw new Error("Unexpected write while polling");
    return path.endsWith("/g1") ? response({ goal: current }) : response({ goals: [current], enabled: true });
  });
  vi.stubGlobal("fetch", fetch);
  render(<GoalsView visible />);
  fireEvent.click(await screen.findByRole("button", { name: /Deliver report/ }));
  expect(await screen.findByText(/1 recorded action in this goal or its children still need verification/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Approve completion" })).toBeDisabled();
  current = { ...current, action_evidence: { pending: 0, confirmed: 1 } };
  await act(async () => { poll?.(); });
  expect(await screen.findByText(/1 recorded action has been verified/)).toHaveTextContent("This alone does not complete the goal");
  expect(screen.queryByText(/Completion cannot be approved yet/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Approve completion" })).toBeEnabled();
  expect(fetch.mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});

it("qualifies a recorded complete status when unresolved effects remain", async () => {
  const current = { ...goal, status: "complete", action_evidence: { pending: 2, confirmed: 1 } };
  vi.stubGlobal("fetch", vi.fn((path: string) => path.endsWith("/g1")
    ? response({ goal: current }) : response({ goals: [current], enabled: true })));
  render(<GoalsView visible />);
  fireEvent.click(await screen.findByRole("button", { name: /Deliver report/ }));
  await waitFor(() => expect(screen.getAllByText(/Marked complete; action verification pending/)).toHaveLength(2));
  expect(screen.getByText(/2 recorded actions in this goal or its children still need verification/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve completion" })).not.toBeInTheDocument();
});


it("shows unresolved family actions in the list without opening details", async () => {
  const current = { ...goal, action_evidence: { pending: 2, confirmed: 0 } };
  const fetch = vi.fn(() => response({ goals: [current], enabled: true }));
  vi.stubGlobal("fetch", fetch);
  render(<GoalsView visible />);
  expect(await screen.findByRole("button", { name: /2 actions await verification across this goal and its children/ })).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Goal details" })).not.toBeInTheDocument();
  expect(fetch.mock.calls).toHaveLength(1);
});
