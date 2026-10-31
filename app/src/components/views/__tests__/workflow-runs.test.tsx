/**
 * A workflow's runs, listed under its row in the Workflows section: each run's
 * status, when it started, how long it took and how many steps finished or
 * failed. Selecting one opens the run-detail drawer.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkflowRunsList } from "../automations/workflow-runs";
import { WorkflowsSection } from "../automations/workflows-section";
import {
  FAILED_RUN_DETAIL,
  FAILED_RUN_ID,
  OK_RUN_ID,
  RUNS,
  WORKFLOW_ID,
} from "./workflow-run-fixtures";

afterEach(() => vi.restoreAllMocks());

function reply(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as Response;
}

describe("WorkflowRunsList", () => {
  it("lists each run with its status, duration and step counts (AC1)", async () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue(reply(RUNS));
    render(<WorkflowRunsList workflowId={WORKFLOW_ID} onOpenRun={() => {}} />);

    const failed = await screen.findByTestId(`workflow-run-${FAILED_RUN_ID}`);
    expect(spy).toHaveBeenCalledWith(`/api/bridge/api/workflows/${WORKFLOW_ID}/runs`);
    expect(within(failed).getByTestId("status-badge").textContent).toContain("failed");
    expect(failed.textContent).toContain("4.2 s");
    expect(within(failed).getByTestId(`workflow-run-steps-${FAILED_RUN_ID}`).textContent).toBe(
      "1 ok, 1 failed, 1 skipped"
    );

    const ok = screen.getByTestId(`workflow-run-${OK_RUN_ID}`);
    expect(within(ok).getByTestId("status-badge").textContent).toContain("completed");
    expect(ok.textContent).toContain("850 ms");
    expect(within(ok).getByTestId(`workflow-run-steps-${OK_RUN_ID}`).textContent).toBe(
      "2 ok, 0 failed"
    );
  });

  it("shows when each run started", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(RUNS));
    render(<WorkflowRunsList workflowId={WORKFLOW_ID} onOpenRun={() => {}} />);
    const started = await screen.findByTestId(`workflow-run-started-${FAILED_RUN_ID}`);
    expect(started.textContent).toMatch(/ago|just now/);
    expect(started.getAttribute("dateTime")).toBe(RUNS[0].started_at);
  });

  it("hands the selected run to the caller", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(RUNS));
    const onOpenRun = vi.fn();
    render(<WorkflowRunsList workflowId={WORKFLOW_ID} onOpenRun={onOpenRun} />);
    fireEvent.click(await screen.findByTestId(`workflow-run-${FAILED_RUN_ID}`));
    expect(onOpenRun).toHaveBeenCalledWith(FAILED_RUN_ID);
  });

  it("has a loading state", () => {
    vi.spyOn(global, "fetch").mockReturnValue(new Promise(() => {}) as never);
    render(<WorkflowRunsList workflowId={WORKFLOW_ID} onOpenRun={() => {}} />);
    expect(screen.getByTestId("workflow-runs-loading")).toBeTruthy();
  });

  it("has an empty state", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply([]));
    render(<WorkflowRunsList workflowId={WORKFLOW_ID} onOpenRun={() => {}} />);
    expect(await screen.findByTestId("workflow-runs-empty")).toBeTruthy();
  });

  it("says an operator-only refusal is one", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply({ detail: "forbidden" }, 403));
    render(<WorkflowRunsList workflowId={WORKFLOW_ID} onOpenRun={() => {}} />);
    expect((await screen.findByTestId("workflow-runs-error")).textContent).toMatch(
      /Operator access required/i
    );
  });

  it("says when the bridge cannot be reached", async () => {
    vi.spyOn(global, "fetch").mockRejectedValue(new TypeError("network"));
    render(<WorkflowRunsList workflowId={WORKFLOW_ID} onOpenRun={() => {}} />);
    expect((await screen.findByTestId("workflow-runs-error")).textContent).toMatch(
      /Could not reach the bridge/i
    );
  });
});

describe("WorkflowsSection runs", () => {
  const LISTING = [
    { workflow_id: WORKFLOW_ID, runs: 2, last_status: "failed", failures: 1, source: "workspace" },
    { workflow_id: "never-ran", runs: 0, last_status: null, failures: 0, source: "workspace" },
  ];

  function routeFetch() {
    return vi.spyOn(global, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/bridge/api/workflows") return reply(LISTING);
      if (url === `/api/bridge/api/workflows/${WORKFLOW_ID}/runs`) return reply(RUNS);
      if (url === `/api/bridge/api/workflows/${WORKFLOW_ID}/runs/${FAILED_RUN_ID}`)
        return reply(FAILED_RUN_DETAIL);
      return reply({ detail: "not found" }, 404);
    });
  }

  it("offers the runs of a workflow that has run, and not of one that never has", async () => {
    routeFetch();
    render(<WorkflowsSection visible />);
    expect(await screen.findByTestId(`workflow-runs-toggle-${WORKFLOW_ID}`)).toBeTruthy();
    expect(screen.queryByTestId("workflow-runs-toggle-never-ran")).toBeNull();
  });

  it("does not fetch runs until they are asked for", async () => {
    const spy = routeFetch();
    render(<WorkflowsSection visible />);
    await screen.findByTestId(`workflow-runs-toggle-${WORKFLOW_ID}`);
    expect(spy.mock.calls.map((c) => String(c[0]))).toEqual(["/api/bridge/api/workflows"]);
  });

  it("expands a workflow's runs and opens one in the run-detail drawer", async () => {
    routeFetch();
    render(<WorkflowsSection visible />);
    const toggle = await screen.findByTestId(`workflow-runs-toggle-${WORKFLOW_ID}`);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(await screen.findByTestId(`workflow-run-${FAILED_RUN_ID}`));
    const drawer = await screen.findByTestId("workflow-run-drawer");
    expect(await within(drawer).findByTestId("run-step-update_record")).toBeTruthy();
  });
});
