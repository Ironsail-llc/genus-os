/**
 * Workflows, now the lower half of the Automations view.
 *
 * The claims are the ones the old `WorkflowsView` test made — it lists what has
 * run, it states the run-history limitation out loud, it does not touch the
 * bridge while hidden, and it has a loading and an empty state. Only the
 * component they are made about moved.
 */
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { WorkflowsSection } from "../automations/workflows-section";

afterEach(() => vi.restoreAllMocks());

describe("WorkflowsSection", () => {
  it("lists workflows and states the run-history limitation", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => [{ workflow_id: "intel", runs: 3, last_status: "completed", failures: 0 }],
    } as Response);
    render(<WorkflowsSection visible />);
    expect(await screen.findByTestId("workflow-row-intel")).toBeTruthy();
    expect(screen.getByTestId("workflows-section").textContent).toMatch(/run at least once/i);
  });

  it("does not fetch when hidden", () => {
    const spy = vi
      .spyOn(global, "fetch")
      .mockResolvedValue({ ok: true, json: async () => [] } as Response);
    render(<WorkflowsSection visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("WorkflowsSection states", () => {
  it("shows a loading skeleton while fetching", () => {
    vi.spyOn(global, "fetch").mockReturnValue(new Promise(() => {}) as never);
    render(<WorkflowsSection visible />);
    expect(screen.getByTestId("workflows-loading")).toBeTruthy();
  });

  it("shows an empty state when there is nothing to list", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] } as Response);
    render(<WorkflowsSection visible />);
    expect(await screen.findByTestId("workflows-empty")).toBeTruthy();
  });

  it("an operator-only refusal says so rather than rendering an empty list", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({ ok: false, status: 403 } as Response);
    render(<WorkflowsSection visible />);
    expect(await screen.findByText(/Operator access required/i)).toBeTruthy();
  });
});
