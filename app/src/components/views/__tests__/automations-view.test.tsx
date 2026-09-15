/**
 * Automations, as the operator drives it.
 *
 * The claims under test are the ones that were wrong before this view existed:
 * that a scheduled agent shows whether it RAN, whether the answer was
 * DELIVERED and whether the work was any good — three separate cells, because
 * the single status pill that preceded them reported a run whose answer never
 * left the box as green.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AutomationsView } from "../automations-view";
import type { Automation } from "@/lib/automations/run-truth";

const NIGHTLY: Automation = {
  id: "invoice-chaser",
  name: "Invoice Chaser",
  kind: "agent",
  description: "Chases unpaid invoices.",
  cron: "0 9 * * *",
  timezone: "UTC",
  enabled: true,
  next_run_at: "2026-06-16T09:00:00Z",
  last_run: {
    id: "11111111-1111-4111-8111-111111111111",
    started_at: "2026-06-15T09:00:00Z",
    status: "completed",
    duration_ms: 4200,
    delivery_status: "delivered",
    delivered_at: "2026-06-15T09:01:00Z",
    delivery_channel: "telegram",
    delivery_mode: "announce",
    verified_status: "verified",
    outcome_assessment: "successful",
  },
  consecutive_errors: 0,
  breaker_tripped: false,
  breaker_threshold: 5,
  delivery: { mode: "announce", channel: "telegram", to: "agent@example.com" },
};

const TRIPPED: Automation = {
  ...NIGHTLY,
  id: "ledger-sweeper",
  name: "Ledger Sweeper",
  description: "",
  last_run: {
    ...NIGHTLY.last_run!,
    id: "22222222-2222-4222-8222-222222222222",
    status: "failed",
    delivery_status: null,
    delivered_at: null,
    verified_status: null,
    outcome_assessment: null,
  },
  consecutive_errors: 5,
  breaker_tripped: true,
  delivery: { mode: "none", channel: "", to: "" },
};

interface Call {
  url: string;
  method: string;
  body: unknown;
}

function mockBridge(rows: Automation[] = [NIGHTLY, TRIPPED]) {
  const calls: Call[] = [];
  const state = { rows };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });

    if (url.includes("/api/workflows")) {
      return { ok: true, status: 200, json: async () => [] } as Response;
    }
    if (url.includes("/api/automations") && method === "GET") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ automations: state.rows, count: state.rows.length }),
      } as Response;
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, state, fetchMock };
}

function callsTo(calls: Call[], fragment: string, method = "POST") {
  return calls.filter((call) => call.url.includes(fragment) && call.method === method);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-06-15T12:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderView(props: Partial<React.ComponentProps<typeof AutomationsView>> = {}) {
  render(<AutomationsView visible role="owner" roleLoading={false} {...props} />);
  await screen.findByTestId(`automation-card-${NIGHTLY.id}`);
}

describe("AutomationsView", () => {
  it("shows a card per scheduled agent with its schedule in words", async () => {
    mockBridge();
    await renderView();

    expect(screen.getByTestId(`automation-name-${NIGHTLY.id}`).textContent).toContain(
      "Invoice Chaser"
    );
    // B8's describeCron, not a second phrasing of the same expression.
    expect(screen.getByTestId(`automation-cron-${NIGHTLY.id}`).textContent).toMatch(/9:00 AM/i);
    expect(screen.getByTestId(`automation-cron-${NIGHTLY.id}`).textContent).toContain("UTC");
    expect(screen.getByTestId(`automation-next-${NIGHTLY.id}`).textContent).toMatch(/Jun 16/);
  });

  it("answers ran, delivered and completed as three separate cells", async () => {
    mockBridge();
    await renderView();

    expect(screen.getByTestId(`automation-ran-${NIGHTLY.id}`).textContent).toContain("completed");
    expect(screen.getByTestId(`automation-delivered-${NIGHTLY.id}`).textContent).toContain(
      "telegram"
    );
    expect(screen.getByTestId(`automation-completed-${NIGHTLY.id}`).textContent).toContain(
      "verified"
    );
  });

  it("a silent agent reads as none expected, and an undelivered run as not delivered", async () => {
    mockBridge();
    await renderView();

    // delivery.mode === "none": deliberate silence is not a failure.
    expect(screen.getByTestId(`automation-delivered-${TRIPPED.id}`).textContent).toContain(
      "none expected"
    );
    expect(screen.getByTestId(`automation-completed-${TRIPPED.id}`).textContent).toContain(
      "not assessed"
    );
    expect(screen.getByTestId(`automation-ran-${TRIPPED.id}`).textContent).toContain("failed");
  });

  it("an automation that has never run says so in all three cells", async () => {
    mockBridge([{ ...NIGHTLY, last_run: null }]);
    await renderView();

    expect(screen.getByTestId(`automation-ran-${NIGHTLY.id}`).textContent).toContain("never run");
    expect(screen.getByTestId(`automation-completed-${NIGHTLY.id}`).textContent).toContain(
      "no run to assess"
    );
  });

  it("the Enabled toggle posts to enable or disable on the manifest route", async () => {
    const { calls } = mockBridge();
    await renderView();

    fireEvent.click(screen.getByTestId(`automation-toggle-${NIGHTLY.id}`));
    await waitFor(() =>
      expect(callsTo(calls, `/api/agent-manifests/${NIGHTLY.id}/disable`)).toHaveLength(1)
    );
  });

  it("a disabled automation offers to enable it", async () => {
    const { calls } = mockBridge([{ ...NIGHTLY, enabled: false }]);
    await renderView();

    fireEvent.click(screen.getByTestId(`automation-toggle-${NIGHTLY.id}`));
    await waitFor(() =>
      expect(callsTo(calls, `/api/agent-manifests/${NIGHTLY.id}/enable`)).toHaveLength(1)
    );
  });

  it("Run now triggers the agent through the engine", async () => {
    const { calls } = mockBridge();
    await renderView();

    fireEvent.click(screen.getByTestId(`automation-run-${NIGHTLY.id}`));
    await waitFor(() =>
      expect(callsTo(calls, `/api/agent-manifests/${NIGHTLY.id}/run`)).toHaveLength(1)
    );
  });

  it("shows a breaker chip only on the tripped automation, and resets it", async () => {
    const { calls } = mockBridge();
    await renderView();

    expect(screen.queryByTestId(`automation-breaker-${NIGHTLY.id}`)).toBeNull();
    const chip = screen.getByTestId(`automation-breaker-${TRIPPED.id}`);
    expect(chip.textContent).toContain("5");

    fireEvent.click(screen.getByTestId(`automation-reset-${TRIPPED.id}`));
    await waitFor(() =>
      expect(callsTo(calls, `/api/automations/${TRIPPED.id}/reset-breaker`)).toHaveLength(1)
    );
  });

  it("edit schedule PATCHes the manifest with a cron, a timezone and a change note", async () => {
    const { calls } = mockBridge();
    await renderView();

    fireEvent.click(screen.getByTestId(`automation-edit-${NIGHTLY.id}`));
    fireEvent.change(screen.getByTestId(`automation-cron-input-${NIGHTLY.id}`), {
      target: { value: "30 7 * * 1-5" },
    });
    fireEvent.change(screen.getByTestId(`automation-timezone-input-${NIGHTLY.id}`), {
      target: { value: "Europe/London" },
    });
    fireEvent.change(screen.getByTestId(`automation-change-input-${NIGHTLY.id}`), {
      target: { value: "Moved to weekday mornings." },
    });
    fireEvent.click(screen.getByTestId(`automation-save-${NIGHTLY.id}`));

    await waitFor(() => {
      const patches = callsTo(calls, `/api/agent-manifests/${NIGHTLY.id}`, "PATCH");
      expect(patches).toHaveLength(1);
      expect(patches[0].body).toEqual({
        cron: "30 7 * * 1-5",
        timezone: "Europe/London",
        change: "Moved to weekday mornings.",
      });
    });
  });

  it("the edit form previews the schedule it is about to save", async () => {
    mockBridge();
    await renderView();

    fireEvent.click(screen.getByTestId(`automation-edit-${NIGHTLY.id}`));
    fireEvent.change(screen.getByTestId(`automation-cron-input-${NIGHTLY.id}`), {
      target: { value: "30 7 * * 1-5" },
    });
    expect(screen.getByTestId(`automation-edit-preview-${NIGHTLY.id}`).textContent).toMatch(
      /7:30 AM/i
    );
  });

  it("refuses to save an expression the engine would reject", async () => {
    const { calls } = mockBridge();
    await renderView();

    fireEvent.click(screen.getByTestId(`automation-edit-${NIGHTLY.id}`));
    fireEvent.change(screen.getByTestId(`automation-cron-input-${NIGHTLY.id}`), {
      target: { value: "0 9 * * * *" },
    });
    expect(screen.getByTestId(`automation-save-${NIGHTLY.id}`)).toHaveProperty("disabled", true);
    expect(callsTo(calls, `/api/agent-manifests/${NIGHTLY.id}`, "PATCH")).toHaveLength(0);
  });

  it("a member sees the cards and none of the buttons that change them", async () => {
    mockBridge();
    render(<AutomationsView visible role="member" roleLoading={false} />);
    await screen.findByTestId(`automation-card-${NIGHTLY.id}`);

    expect(screen.getByTestId(`automation-ran-${NIGHTLY.id}`)).toBeTruthy();
    expect(screen.queryByTestId(`automation-toggle-${NIGHTLY.id}`)).toBeNull();
    expect(screen.queryByTestId(`automation-run-${NIGHTLY.id}`)).toBeNull();
    expect(screen.queryByTestId(`automation-edit-${NIGHTLY.id}`)).toBeNull();
    expect(screen.queryByTestId(`automation-reset-${TRIPPED.id}`)).toBeNull();
    expect(screen.getByTestId("automations-readonly")).toBeTruthy();
  });

  it("an unresolved session is not a refusal", async () => {
    mockBridge();
    render(<AutomationsView visible role={null} roleLoading />);
    await screen.findByTestId(`automation-card-${NIGHTLY.id}`);
    expect(screen.getByTestId(`automation-toggle-${NIGHTLY.id}`)).toBeTruthy();
  });

  it("tells a non-operator the listing is not theirs rather than showing a red error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/api/automations")
          ? ({ ok: false, status: 403, json: async () => ({}) } as Response)
          : ({ ok: true, status: 200, json: async () => [] } as Response)
      )
    );
    render(<AutomationsView visible role="member" roleLoading={false} />);
    expect(await screen.findByTestId("automations-forbidden")).toBeTruthy();
    expect(screen.queryByTestId("automations-error")).toBeNull();
  });

  it("surfaces the bridge's own words when the listing fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/api/automations")
          ? ({
              ok: false,
              status: 500,
              json: async () => ({ detail: "the manifest directory is unreadable" }),
            } as Response)
          : ({ ok: true, status: 200, json: async () => [] } as Response)
      )
    );
    render(<AutomationsView visible role="owner" roleLoading={false} />);
    const error = await screen.findByTestId("automations-error");
    expect(error.textContent).toContain("the manifest directory is unreadable");
  });

  it("shows an empty state when nothing is scheduled", async () => {
    mockBridge([]);
    render(<AutomationsView visible role="owner" roleLoading={false} />);
    expect(await screen.findByTestId("automations-empty")).toBeTruthy();
  });

  it("keeps the Workflows section below the automations", async () => {
    mockBridge();
    await renderView();
    expect(screen.getByTestId("workflows-section")).toBeTruthy();
  });
});

describe("AutomationsView polling", () => {
  it("does not touch the bridge while the view is hidden", () => {
    const { fetchMock } = mockBridge();
    render(<AutomationsView visible={false} role="owner" roleLoading={false} />);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("re-reads the listing every 60 seconds while visible", async () => {
    const { calls } = mockBridge();
    await renderView();
    expect(callsTo(calls, "/api/automations", "GET")).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(callsTo(calls, "/api/automations", "GET")).toHaveLength(2));
  });

  it("stops polling once the view is hidden", async () => {
    const { calls } = mockBridge();
    const { rerender } = render(
      <AutomationsView visible role="owner" roleLoading={false} />
    );
    await screen.findByTestId(`automation-card-${NIGHTLY.id}`);

    rerender(<AutomationsView visible={false} role="owner" roleLoading={false} />);
    const before = callsTo(calls, "/api/automations", "GET").length;
    await vi.advanceTimersByTimeAsync(180_000);
    expect(callsTo(calls, "/api/automations", "GET")).toHaveLength(before);
  });

  it("Refresh re-reads on demand", async () => {
    const { calls } = mockBridge();
    await renderView();
    fireEvent.click(screen.getByTestId("automations-refresh"));
    await waitFor(() => expect(callsTo(calls, "/api/automations", "GET").length).toBeGreaterThan(1));
  });
});
