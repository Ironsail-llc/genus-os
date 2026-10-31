import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { RunsView } from "../runs-view";

afterEach(() => vi.restoreAllMocks());

/**
 * The list URL now carries its filters and page (`/api/runs?limit=50&…`), so
 * the mocks match the path rather than the end of the string.
 */
const isList = (u: string) => /\/api\/runs(\?|$)/.test(u);

describe("RunsView", () => {
  it("lists runs and opens detail with guardrail blocks flagged", async () => {
    vi.spyOn(global, "fetch").mockImplementation((url: string | URL | Request) => {
      const u = String(url);
      if (isList(u)) {
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
      if (isList(u)) {
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

describe("RunsView run truth", () => {
  const RUN = {
    id: "r1",
    agent_id: "main",
    status: "completed",
    delivery_status: "failed",
    delivered_at: null,
    delivery_channel: "telegram",
    verified_status: "unverified_claims",
  };

  function mockRuns() {
    vi.spyOn(global, "fetch").mockImplementation((url: string | URL | Request) =>
      isList(String(url))
        ? (Promise.resolve({ ok: true, json: async () => [RUN] } as Response) as never)
        : (Promise.resolve({
            ok: true,
            json: async () => ({ run: RUN, steps: [], guardrail_events: [] }),
          } as Response) as never)
    );
  }

  it("says on the row whether the answer was delivered", async () => {
    mockRuns();
    render(<RunsView visible />);
    // `completed` with a failed delivery is exactly the run the status pill
    // alone reported as green.
    const delivered = await screen.findByTestId("run-delivered-r1");
    expect(delivered.textContent).toMatch(/failed/i);
  });

  it("dates a successful delivery — when it landed, not only that it did", async () => {
    const landed = {
      ...RUN,
      id: "r3",
      delivery_status: "delivered",
      delivered_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
    };
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => [landed],
    } as Response);
    render(<RunsView visible />);
    const delivered = await screen.findByTestId("run-delivered-r3");
    expect(delivered.textContent).toMatch(/2 hours ago/);
  });

  it("reads the delivery status case-insensitively, as run-truth.ts does", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => [{ id: "r4", agent_id: "main", delivery_status: "Delivered" }],
    } as Response);
    render(<RunsView visible />);
    // Two readers of one column that disagree on case are two answers.
    const delivered = await screen.findByTestId("run-delivered-r4");
    expect(delivered.className).not.toMatch(/destructive/);
  });

  it("shows the verification verdict on the detail", async () => {
    mockRuns();
    render(<RunsView visible />);
    fireEvent.click(await screen.findByTestId("run-row-r1"));
    const verified = await screen.findByTestId("run-verified-r1");
    expect(verified.textContent).toMatch(/unverified claims/i);
  });

  it("a run with no delivery recorded does not claim one failed", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => [{ id: "r2", agent_id: "worker", status: "completed" }],
    } as Response);
    render(<RunsView visible />);
    await screen.findByTestId("run-row-r2");
    expect(screen.queryByTestId("run-delivered-r2")).toBeNull();
  });
});

const SUMMARY = {
  since_hours: 168,
  total: 6,
  by_status: { completed: 4, failed: 1, timeout: 1 },
  by_delivery: { delivered: 3, none_expected: 2, failed: 1, pending: 0, not_recorded: 0 },
  by_agent: [
    { agent_id: "main", runs: 4, failed: 2, cost_usd: 0.3 },
    { agent_id: "example-agent", runs: 2, failed: 0, cost_usd: 0.05 },
  ],
  total_cost_usd: 0.35,
};

const ROW = {
  id: "r1",
  agent_id: "main",
  trigger_type: "cron",
  status: "completed",
  started_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
  duration_ms: 4200,
  model_used: "example-model",
  input_tokens: 1200,
  output_tokens: 300,
  total_cost_usd: 0.0123,
  delivery_status: "silent",
  verified_status: "verified",
};

function mockPage(rows: unknown[] = [ROW], summary: unknown = SUMMARY) {
  return vi.spyOn(global, "fetch").mockImplementation((url: string | URL | Request) => {
    const u = String(url);
    if (u.includes("/api/runs/summary")) {
      return Promise.resolve({ ok: true, json: async () => summary } as Response) as never;
    }
    if (isList(u)) {
      return Promise.resolve({ ok: true, json: async () => rows } as Response) as never;
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({ run: ROW, steps: [], guardrail_events: [] }),
    } as Response) as never;
  });
}

const calls = (spy: ReturnType<typeof mockPage>, part: string) =>
  spy.mock.calls.map((c) => String(c[0])).filter((u) => u.includes(part));

describe("RunsView page", () => {
  it("shows the summary strip over the period", async () => {
    mockPage();
    render(<RunsView visible />);
    const strip = await screen.findByTestId("runs-summary");
    await waitFor(() => expect(strip.textContent).toMatch(/6/));
    expect(strip.textContent).toMatch(/Runs/);
    expect(strip.textContent).toMatch(/Failed/);
    expect(strip.textContent).toMatch(/2/); // failed + timeout
    expect(strip.textContent).toMatch(/Delivered/);
    expect(strip.textContent).toMatch(/3/);
    expect(strip.textContent).toMatch(/\$0\.35/);
  });

  it("renders a full-width table with every column", async () => {
    mockPage();
    render(<RunsView visible />);
    const table = await screen.findByTestId("runs-table");
    for (const header of [
      "Agent",
      "Trigger",
      "Status",
      "Started",
      "Duration",
      "Model",
      "Tokens",
      "Cost",
      "Delivery",
      "Verification",
    ]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeTruthy();
    }
    const row = await screen.findByTestId("run-row-r1");
    expect(row.textContent).toMatch(/cron/);
    expect(row.textContent).toMatch(/example-model/);
    expect(row.textContent).toMatch(/4\.2 s/);
    expect(row.textContent).toMatch(/1,500/);
    expect(row.textContent).toMatch(/\$0\.0123/);
    expect(screen.getByTestId("run-verification-r1").textContent).toMatch(/verified/);
  });

  it("reads an intentional non-send as no delivery expected, in a neutral tone", async () => {
    mockPage([
      { ...ROW, id: "quiet", delivery_status: "suppressed_trivial" },
      { ...ROW, id: "broken", delivery_status: "failed:telegram_send" },
    ]);
    render(<RunsView visible />);
    const quiet = await screen.findByTestId("run-delivered-quiet");
    expect(quiet.textContent).toMatch(/no delivery expected/i);
    expect(quiet.className).not.toMatch(/destructive/);
    const broken = screen.getByTestId("run-delivered-broken");
    expect(broken.className).toMatch(/destructive/);
  });

  it("asks for the default period, and refetches list and summary when a filter changes", async () => {
    const spy = mockPage();
    render(<RunsView visible />);
    await screen.findByTestId("run-row-r1");
    expect(calls(spy, "/api/runs?")[0]).toMatch(/since=168/);
    expect(calls(spy, "/api/runs/summary")[0]).toMatch(/since=168/);

    fireEvent.change(screen.getByTestId("runs-filter-status"), { target: { value: "failed" } });
    await waitFor(() => expect(calls(spy, "status=failed").length).toBeGreaterThanOrEqual(2));
    expect(calls(spy, "/api/runs?").at(-1)).toMatch(/status=failed/);
    expect(calls(spy, "/api/runs/summary").at(-1)).toMatch(/status=failed/);

    // The agent options come from the summary.
    await waitFor(() =>
      expect(within(screen.getByTestId("runs-filter-agent")).getByRole("option", { name: "example-agent" })).toBeTruthy()
    );
    fireEvent.change(screen.getByTestId("runs-filter-agent"), { target: { value: "example-agent" } });
    await waitFor(() => expect(calls(spy, "agent=example-agent").length).toBeGreaterThanOrEqual(2));

    fireEvent.change(screen.getByTestId("runs-filter-trigger"), { target: { value: "cron" } });
    await waitFor(() => expect(calls(spy, "trigger=cron").length).toBeGreaterThanOrEqual(2));

    fireEvent.change(screen.getByTestId("runs-filter-period"), { target: { value: "all" } });
    await waitFor(() => expect(calls(spy, "/api/runs?").at(-1)).not.toMatch(/since=/));
    expect(calls(spy, "/api/runs/summary").at(-1)).not.toMatch(/since=/);
  });

  it("loads the next page when the last one was full", async () => {
    const page = Array.from({ length: 50 }, (_, i) => ({ ...ROW, id: `p${i}` }));
    const spy = mockPage(page);
    render(<RunsView visible />);
    const more = await screen.findByTestId("runs-more");
    spy.mockImplementation((url: string | URL | Request) => {
      const u = String(url);
      if (u.includes("/api/runs/summary")) {
        return Promise.resolve({ ok: true, json: async () => SUMMARY } as Response) as never;
      }
      return Promise.resolve({
        ok: true,
        json: async () => [{ ...ROW, id: "next" }],
      } as Response) as never;
    });
    fireEvent.click(more);
    expect(await screen.findByTestId("run-row-next")).toBeTruthy();
    expect(screen.getByTestId("run-row-p0")).toBeTruthy();
    expect(calls(spy, "offset=50").length).toBe(1);
    await waitFor(() => expect(screen.queryByTestId("runs-more")).toBeNull());
  });

  it("keeps rows unique when the next page repeats one already shown", async () => {
    // New runs start at the top between pages, so an offset page can overlap.
    const page = Array.from({ length: 50 }, (_, i) => ({ ...ROW, id: `p${i}` }));
    const spy = mockPage(page);
    render(<RunsView visible />);
    const more = await screen.findByTestId("runs-more");
    spy.mockImplementation((url: string | URL | Request) => {
      const u = String(url);
      if (u.includes("/api/runs/summary")) {
        return Promise.resolve({ ok: true, json: async () => SUMMARY } as Response) as never;
      }
      return Promise.resolve({
        ok: true,
        json: async () => [{ ...ROW, id: "p49" }, { ...ROW, id: "next" }],
      } as Response) as never;
    });
    fireEvent.click(more);
    expect(await screen.findByTestId("run-row-next")).toBeTruthy();
    expect(screen.getAllByTestId("run-row-p49")).toHaveLength(1);
  });

  it("does not offer more when the page was not full", async () => {
    mockPage();
    render(<RunsView visible />);
    await screen.findByTestId("run-row-r1");
    expect(screen.queryByTestId("runs-more")).toBeNull();
  });

  it("keeps the table inside the page: the wrapper scrolls, the page does not", async () => {
    mockPage();
    render(<RunsView visible />);
    const table = await screen.findByTestId("runs-table");
    expect(table.parentElement?.className).toMatch(/overflow-x-auto/);
    expect(screen.getByTestId("runs-view").className).toMatch(/min-w-0/);
  });

  it("opens the run in a drawer when a row is selected", async () => {
    mockPage();
    render(<RunsView visible />);
    fireEvent.click(await screen.findByTestId("run-row-r1"));
    expect(await screen.findByTestId("run-detail")).toBeTruthy();
  });
});
