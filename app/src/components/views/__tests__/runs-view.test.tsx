import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { RunsView } from "../runs-view";

afterEach(() => vi.restoreAllMocks());

describe("RunsView", () => {
  it("lists runs and opens detail with guardrail blocks flagged", async () => {
    vi.spyOn(global, "fetch").mockImplementation((url: string | URL | Request) => {
      const u = String(url);
      if (u.endsWith("/api/runs")) {
        return Promise.resolve({ ok: true, json: async () => [
          { id: "r1", agent_id: "main", status: "completed", total_cost_usd: 0.01 },
        ] } as Response);
      }
      return Promise.resolve({ ok: true, json: async () => ({
        run: { id: "r1", agent_id: "main", status: "completed" },
        steps: [{ step_number: 1, step_type: "tool_call", tool_name: "exec" }],
        guardrail_events: [{ guardrail_name: "exec_allowlist_strict", action: "blocked", tool_name: "exec" }],
      }) } as Response);
    });
    render(<RunsView visible />);
    const row = await screen.findByTestId("run-row-r1");
    fireEvent.click(row);
    const detail = await screen.findByTestId("run-detail");
    expect(detail.textContent).toMatch(/blocked/i);
    const block = await screen.findByTestId("guardrail-event-0");
    expect(block.className).toMatch(/warning/i);
  });

  it("marks a retried LLM attempt as an attempt, not a second call", async () => {
    vi.spyOn(global, "fetch").mockImplementation((url: string | URL | Request) => {
      const u = String(url);
      if (u.endsWith("/api/runs")) {
        return Promise.resolve({ ok: true, json: async () => [
          { id: "r1", agent_id: "main", status: "completed" },
        ] } as Response);
      }
      return Promise.resolve({ ok: true, json: async () => ({
        run: { id: "r1", agent_id: "main", status: "completed" },
        steps: [
          { step_number: 1, step_type: "llm_call", error_message: "reasoning_only_retry: finish_reason=length" },
          { step_number: 2, step_type: "llm_call" },
        ],
        guardrail_events: [],
      }) } as Response);
    });
    render(<RunsView visible />);
    fireEvent.click(await screen.findByTestId("run-row-r1"));
    const marker = await screen.findByTestId("attempt-1");
    expect(marker.textContent).toMatch(/attempt failed/i);
    expect(marker.textContent).toMatch(/reasoning_only_retry/);
    expect(screen.queryByTestId("attempt-2")).toBeNull();
  });

  it("does not fetch when hidden", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] } as Response);
    render(<RunsView visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("RunsView states", () => {
  it("shows a loading skeleton while fetching", () => {
    vi.spyOn(global, "fetch").mockReturnValue(new Promise(() => {}) as never);
    render(<RunsView visible />);
    expect(screen.getByTestId("runs-loading")).toBeTruthy();
  });

  it("shows an empty state when there is nothing to list", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] } as Response);
    render(<RunsView visible />);
    expect(await screen.findByTestId("runs-empty")).toBeTruthy();
  });
});
