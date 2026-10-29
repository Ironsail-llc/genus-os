/**
 * Workflows, now the lower half of the Automations view.
 *
 * It lists every workflow the bridge reports — including one defined in
 * `docs/workflows/` that has never run, labelled with where it came from — it
 * does not touch the bridge while hidden, and it has a loading and an empty
 * state.
 */
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { WorkflowsSection } from "../automations/workflows-section";

afterEach(() => vi.restoreAllMocks());

describe("WorkflowsSection", () => {
  it("lists workflows, including one defined but never run, with where each came from", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => [
        {
          workflow_id: "intel",
          runs: 3,
          last_status: "completed",
          failures: 0,
          source: "workspace",
        },
        {
          workflow_id: "desk-pass",
          runs: 0,
          last_status: null,
          failures: 0,
          source: "plugin:acme-desk",
        },
      ],
    } as Response);
    render(<WorkflowsSection visible />);
    expect(await screen.findByTestId("workflow-row-intel")).toBeTruthy();
    const neverRun = screen.getByTestId("workflow-row-desk-pass");
    expect(neverRun.textContent).toContain("never run");
    expect(screen.getByTestId("workflow-source-desk-pass").textContent).toContain(
      "plugin:acme-desk"
    );
    expect(screen.getByTestId("workflow-source-intel").textContent).toContain("workspace");
    // The old caveat is gone: never-run workflows are listed now.
    expect(screen.getByTestId("workflows-section").textContent).not.toMatch(/run at least once/i);
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
