import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { WorkflowsView } from "../workflows-view";

afterEach(() => vi.restoreAllMocks());

describe("WorkflowsView", () => {
  it("lists workflows and states the run-history limitation", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true, json: async () => [
        { workflow_id: "intel", runs: 3, last_status: "completed", failures: 0 },
      ],
    } as Response);
    render(<WorkflowsView visible />);
    expect(await screen.findByTestId("workflow-row-intel")).toBeTruthy();
    expect(screen.getByTestId("workflows-view").textContent).toMatch(/run at least once/i);
  });

  it("does not fetch when hidden", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] } as Response);
    render(<WorkflowsView visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });
});
