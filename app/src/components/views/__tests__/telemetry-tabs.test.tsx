/**
 * Observe › Logs › Datadog, beyond the log search: the Traces, Monitors and
 * Activity tabs, the trace view, the "View trace" link on a log line, a query
 * handed in by a deep link, and the root operator's organizations summary.
 *
 * Every read is the bridge's (`crm/bridge/routers/telemetry.py`), for the
 * tenant the session acts in. The page never names a tenant, every state is in
 * plain words, and a tab that goes off screen keeps its state and sends
 * nothing when it comes back.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LogsView } from "../logs-view";

const TRACE_ID = "abc123def4567890";

function logRow(id: string, extra: Partial<Record<string, unknown>> = {}) {
  return {
    id,
    timestamp: "2026-11-25T10:00:00Z",
    service: "shop-api",
    status: "error",
    host: "web-1",
    message: "checkout failed",
    tags: [],
    attributes: {},
    ...extra,
  };
}

const LOGS = {
  status: "ok",
  scope: "acme-child-a",
  rows: [
    logRow("log-1", { attributes: { trace_id: TRACE_ID } }),
    logRow("log-2", { attributes: { dd: { trace_id: "1234567890" } } }),
    logRow("log-3", { message: `payment declined trace_id=${TRACE_ID} run_id=r1` }),
    logRow("log-4", { message: "no trace here" }),
    // The bridge's own field, from the event's reserved trace id, comes first.
    logRow("log-5", {
      trace_id: "4bf92f3577b34da6a3ce929d0e0e4736",
      attributes: { trace_id: "1111" },
      message: "trace_id=2222",
    }),
  ],
  next_cursor: null,
};

const SUMMARY = { status: "ok", scope: "acme-child-a", group_by: "service", errors: true, buckets: [] };

const TRACES = {
  status: "ok",
  scope: "acme-child-a",
  rows: [
    {
      trace_id: TRACE_ID,
      span_id: "root-1",
      service: "shop-api",
      resource: "POST /checkout",
      operation: "http.request",
      timestamp: "2026-11-25T10:00:00Z",
      duration_ns: 9_000_000,
      status: "error",
    },
  ],
  next_cursor: null,
};

function span(span_id: string, parent_id: string, resource: string, duration_ns: number, start: string) {
  return {
    trace_id: TRACE_ID,
    span_id,
    parent_id,
    service: "shop-api",
    resource,
    operation: "http.request",
    timestamp: start,
    end: start,
    duration_ns,
    status: span_id === "root-1" ? "error" : "ok",
    env: "prod",
    tags: [],
  };
}

const TRACE = {
  status: "ok",
  scope: "acme-child-a",
  trace_id: TRACE_ID,
  spans: [
    span("root-1", "0", "POST /checkout", 9_000_000, "2026-11-25T10:00:00.000Z"),
    span("child-1", "root-1", "GET /cart", 2_500_000, "2026-11-25T10:00:00.001Z"),
    span("grandchild-1", "child-1", "SELECT carts", 800_000, "2026-11-25T10:00:00.002Z"),
    span("child-2", "root-1", "SELECT orders", 1_200_000, "2026-11-25T10:00:00.004Z"),
  ],
};

const MONITORS_ALL = {
  status: "ok",
  scope: "acme-child-a",
  firing: false,
  monitors: [
    { id: "1", name: "Checkout errors", status: "Alert", firing: true, type: "log alert", last_triggered: "2026-11-25T10:00:00+00:00" },
    { id: "2", name: "Cart latency", status: "OK", firing: false, type: "metric alert", last_triggered: null },
  ],
};

const MONITORS_FIRING = { ...MONITORS_ALL, firing: true, monitors: [MONITORS_ALL.monitors[0]] };

const SIGNALS = {
  status: "ok",
  scope: "acme-child-a",
  signals: [
    {
      id: 12,
      type: "watch",
      timestamp: "2026-11-25T10:01:00+00:00",
      mode: "dry_run",
      sent: false,
      recipients: "main agent alert digest, tenant acme-child-a",
      finding: "error_spike",
      service: "shop-api",
      count: 40,
      window: { start: "2026-11-25T09:45:00+00:00", end: "2026-11-25T10:00:00+00:00", minutes: 15 },
      message: "shop-api logged 40 errors in the 15 minutes to 10:00 UTC.",
    },
    {
      id: 11,
      type: "alert",
      timestamp: "2026-11-25T10:00:05+00:00",
      mode: "dry_run",
      sent: false,
      recipients: "the instance operator, by telegram and email",
      title: "Checkout errors",
      transition: "triggered",
      priority: "P2",
      monitor_id: "1234",
      link: "https://app.example.com/monitors/1234",
      level: "critical",
      would_run: { agent: "main", prompt: "Take a read-only look at this tenant's logs. Change nothing." },
    },
  ],
};

const OVERVIEW = {
  status: "ok",
  since: "1h",
  home_tenant_id: "acme-parent",
  tenants: [
    { tenant_id: "acme-parent", connected: true, errors: 12, firing_monitors: 1, error_kind: null },
    { tenant_id: "acme-child-a", connected: true, errors: 5, firing_monitors: 3, error_kind: null },
    { tenant_id: "acme-child-b", connected: false, errors: null, firing_monitors: null, error_kind: null },
    { tenant_id: "acme-child-c", connected: true, errors: null, firing_monitors: null, error_kind: "rate_limit" },
  ],
};

const ACCESSIBLE = [
  { id: "acme-parent", display_name: "Acme Holdings", parent_tenant_id: null, is_home: true },
  { id: "acme-child-a", display_name: "Acme Shop", parent_tenant_id: "acme-parent", is_home: false },
  { id: "acme-child-b", display_name: "Acme Clinic", parent_tenant_id: "acme-parent", is_home: false },
];

type Reply = { status?: number; body: unknown };

const requests: string[] = [];
const fetchMock = vi.fn();
let overrides: Array<[RegExp, (url: string) => Reply]> = [];

const ROUTES: Array<[RegExp, (url: string) => Reply]> = [
  [/\/api\/telemetry\/logs\/summary\?/, () => ({ body: SUMMARY })],
  [/\/api\/telemetry\/logs\?/, () => ({ body: LOGS })],
  [/\/api\/telemetry\/traces\/[^/?]+/, () => ({ body: TRACE })],
  [/\/api\/telemetry\/traces\?/, () => ({ body: TRACES })],
  [/\/api\/telemetry\/monitors/, (url) => ({ body: url.includes("firing=true") ? MONITORS_FIRING : MONITORS_ALL })],
  [/\/api\/telemetry\/signals/, () => ({ body: SIGNALS })],
  [/\/api\/telemetry\/overview/, () => ({ body: OVERVIEW })],
  [/\/api\/tenants\/accessible/, () => ({ body: ACCESSIBLE })],
];

function stub() {
  fetchMock.mockImplementation(async (url: string) => {
    requests.push(url);
    for (const [pattern, make] of [...overrides, ...ROUTES]) {
      if (pattern.test(url)) {
        const { status = 200, body } = make(url);
        return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
      }
    }
    throw new Error(`no stub for ${url}`);
  });
}

function sent(pattern: RegExp): string[] {
  return requests.filter((u) => pattern.test(u));
}

beforeEach(() => {
  requests.length = 0;
  overrides = [];
  fetchMock.mockReset();
  stub();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function openTab(name: "logs" | "traces" | "monitors" | "activity") {
  fireEvent.click(screen.getByTestId(`telemetry-tab-${name}`));
}

describe("the tabs", () => {
  it("opens on Logs and offers Traces, Monitors and Activity", async () => {
    render(<LogsView role="admin" />);

    await screen.findByTestId("telemetry-table");
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(tabs).toEqual(expect.arrayContaining(["Logs", "Traces", "Monitors", "Activity"]));
    expect(screen.getByTestId("telemetry-tab-logs").getAttribute("aria-selected")).toBe("true");
    // Nothing behind a tab that is not open is read.
    expect(sent(/\/traces|\/monitors|\/signals|\/overview/)).toEqual([]);
  });

  it("keeps a tab's state and sends nothing when it comes back on screen", async () => {
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");

    openTab("monitors");
    await screen.findByTestId("monitors-table");
    openTab("logs");
    await screen.findByTestId("telemetry-table");
    openTab("monitors");
    await screen.findByTestId("monitors-table");

    expect(sent(/\/api\/telemetry\/monitors/)).toHaveLength(1);
    expect(sent(/\/api\/telemetry\/logs\?/)).toHaveLength(1);
  });
});

describe("Traces", () => {
  it("lists the error traces of the window: time, service, resource, duration, status", async () => {
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("traces");

    const table = await screen.findByTestId("traces-table");
    expect(table.textContent).toContain("shop-api");
    expect(table.textContent).toContain("POST /checkout");
    expect(table.textContent).toContain("9 ms");
    expect(table.textContent).toContain("error");
    const url = new URL(sent(/\/api\/telemetry\/traces\?/)[0], "http://x");
    expect(url.searchParams.get("since")).toBe("1h");
    expect(url.searchParams.get("q")).toBe("status:error");
  });

  it("reads another window when the time range changes", async () => {
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("traces");
    await screen.findByTestId("traces-table");

    fireEvent.change(screen.getByTestId("traces-since"), { target: { value: "24h" } });

    await waitFor(() => expect(sent(/\/api\/telemetry\/traces\?/).at(-1)).toContain("since=24h"));
  });

  it("opens a trace as an indented tree of its spans with their durations", async () => {
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("traces");
    fireEvent.click(await screen.findByTestId("traces-row-0"));

    const sheet = await screen.findByTestId("trace-detail");
    await within(sheet).findByTestId("trace-span-root-1");
    expect(sent(/\/api\/telemetry\/traces\/abc123def4567890/)).toHaveLength(1);
    const depth = (id: string) => within(sheet).getByTestId(`trace-span-${id}`).getAttribute("data-depth");
    expect(depth("root-1")).toBe("0");
    expect(depth("child-1")).toBe("1");
    expect(depth("grandchild-1")).toBe("2");
    expect(depth("child-2")).toBe("1");
    // Depth first: a child's own children come before its next sibling.
    const order = within(sheet)
      .getAllByTestId(/^trace-span-/)
      .map((el) => el.getAttribute("data-testid"));
    expect(order).toEqual([
      "trace-span-root-1",
      "trace-span-child-1",
      "trace-span-grandchild-1",
      "trace-span-child-2",
    ]);
    expect(within(sheet).getByTestId("trace-span-child-1").textContent).toContain("2.5 ms");
    expect(within(sheet).getByTestId("trace-span-grandchild-1").textContent).toContain("0.8 ms");
  });

  it("says a trace was not found in plain words", async () => {
    overrides = [
      [/\/api\/telemetry\/traces\/[^/?]+/, () => ({ status: 404, body: { detail: "No trace with that id was found in this organization's data." } })],
    ];
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("traces");
    fireEvent.click(await screen.findByTestId("traces-row-0"));

    const missing = await screen.findByTestId("trace-not-found");
    expect(missing.textContent).toContain("not found in your organization's traces");
    // Most ordinary requests have no trace: the page says why, whatever the bridge's words.
    expect(missing.textContent).toContain("Datadog keeps only a sample of traces");
  });

  it("says when the window has no error trace", async () => {
    overrides = [[/\/api\/telemetry\/traces\?/, () => ({ body: { ...TRACES, rows: [] } })]];
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("traces");

    expect((await screen.findByTestId("traces-empty")).textContent).toMatch(/no error trace/i);
  });

  it.each([
    [{ status: "not_configured", message: "Datadog is not connected for this organization yet." }, "traces-not-configured", "not connected"],
    [{ status: "unavailable", kind: "rate_limit", scope: "acme-child-a" }, "traces-unavailable", "too many requests"],
    [{ status: "no_access", message: "Traces are not shared with this organization.", scope: "acme-child-a" }, "traces-no-access", "not shared"],
  ])("shows the bridge's state in plain words", async (body, testId, words) => {
    overrides = [[/\/api\/telemetry\/traces\?/, () => ({ body })]];
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("traces");

    expect((await screen.findByTestId(testId)).textContent?.toLowerCase()).toContain(words);
  });

  it("shows a refused trace search with the bridge's reason", async () => {
    overrides = [[/\/api\/telemetry\/traces\?/, () => ({ status: 400, body: { detail: "the brackets or parentheses close out of order" } })]];
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("traces");

    expect((await screen.findByTestId("traces-refused")).textContent).toContain("close out of order");
  });
});

describe("View trace from a log line", () => {
  it.each([
    [0, TRACE_ID],
    [1, "1234567890"],
    [2, TRACE_ID],
    [4, "4bf92f3577b34da6a3ce929d0e0e4736"],
  ])("links row %i to its trace and opens it on the Traces tab", async (index, traceId) => {
    render(<LogsView role="admin" />);
    fireEvent.click(await screen.findByTestId(`telemetry-row-${index}`));

    const link = await screen.findByTestId("telemetry-view-trace");
    fireEvent.click(link);

    expect(screen.getByTestId("telemetry-tab-traces").getAttribute("aria-selected")).toBe("true");
    await screen.findByTestId("trace-detail");
    await waitFor(() => expect(sent(new RegExp(`/api/telemetry/traces/${traceId}`))).toHaveLength(1));
  });

  it("offers no link for a line without a trace id", async () => {
    render(<LogsView role="admin" />);
    fireEvent.click(await screen.findByTestId("telemetry-row-3"));

    await screen.findByTestId("telemetry-detail");
    expect(screen.queryByTestId("telemetry-view-trace")).toBeNull();
  });
});

describe("Monitors", () => {
  it("shows firing monitors by default, with their status and last trigger", async () => {
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("monitors");

    const table = await screen.findByTestId("monitors-table");
    expect(sent(/\/api\/telemetry\/monitors/)[0]).toContain("firing=true");
    expect(screen.getByTestId("monitors-firing-toggle").getAttribute("aria-pressed")).toBe("true");
    expect(table.textContent).toContain("Checkout errors");
    expect(table.textContent).toContain("Alert");
    expect(table.textContent).not.toContain("Cart latency");
  });

  it("shows every monitor when firing only is turned off", async () => {
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("monitors");
    await screen.findByTestId("monitors-table");

    fireEvent.click(screen.getByTestId("monitors-firing-toggle"));

    await waitFor(() => expect(screen.getByTestId("monitors-table").textContent).toContain("Cart latency"));
    expect(sent(/\/api\/telemetry\/monitors/).at(-1)).toContain("firing=false");
    expect(screen.getByTestId("monitors-row-2").textContent).toContain("Never");
  });

  it("says nothing is firing when the list is empty", async () => {
    overrides = [[/\/api\/telemetry\/monitors/, () => ({ body: { ...MONITORS_FIRING, monitors: [] } })]];
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("monitors");

    expect((await screen.findByTestId("monitors-empty")).textContent).toMatch(/nothing is firing/i);
  });

  it("says when monitors are not shared with the organization", async () => {
    overrides = [
      [/\/api\/telemetry\/monitors/, () => ({ body: { status: "no_access", message: "Monitors are not shared with this organization.", scope: "acme-child-a" } })],
    ];
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("monitors");

    expect((await screen.findByTestId("monitors-no-access")).textContent).toContain(
      "Monitors are not shared with this organization.",
    );
  });
});

describe("Activity", () => {
  it("lists the recorded alerts and watch findings, each labelled recorded, not sent", async () => {
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("activity");

    const list = await screen.findByTestId("activity-list");
    const alert = within(list).getByTestId("activity-item-11");
    const finding = within(list).getByTestId("activity-item-12");
    for (const item of [alert, finding]) expect(item.textContent).toContain("Recorded, not sent");
    expect(alert.textContent).toContain("Checkout errors");
    expect(alert.textContent).toContain("triggered");
    expect(alert.textContent).toContain("the instance operator, by telegram and email");
    expect(alert.textContent).toContain("Change nothing.");
    expect(finding.textContent).toContain("shop-api logged 40 errors");
    expect(finding.textContent).toContain("main agent alert digest");
    // The page reads the token's tenant; it never names one.
    expect(sent(/\/api\/telemetry\/signals/)[0]).not.toMatch(/tenant/);
  });

  async function activityWith(signals: unknown[]) {
    overrides = [[/\/api\/telemetry\/signals/, () => ({ body: { ...SIGNALS, signals } })]];
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("activity");
    return screen.findByTestId("activity-list");
  }

  it("says a delivered finding was sent, and by which route", async () => {
    await activityWith([
      { ...SIGNALS.signals[0], mode: "delivered", sent: true, reason: null, delivery_route: "alert_digest" },
    ]);

    const item = screen.getByTestId("activity-item-12");
    expect(within(item).getByTestId("activity-status").textContent).toBe("Sent");
    expect(item.textContent).toContain("alert_digest");
    expect(item.textContent).not.toContain("Recorded, not sent");
  });

  it.each([
    ["suppressed", "episode_open", "Not sent again: already reported", "an alert for this is already open"],
    ["suppressed", "cooldown", "Not sent again: already reported", "too soon after the last one"],
    ["suppressed", "covered_by_webhook", "Not sent again: already reported", "already reported by the Datadog alert"],
    ["delivery_failed", "no_engine", "Could not be sent", "this organization's engine is not running"],
    ["delivery_failed", "handoff_failed", "Could not be sent", "the hand-off failed"],
    ["delivery_failed", "rate_limit", "Could not be sent", "rate limit"],
    ["dry_run", null, "Recorded, not sent", null],
    ["something_new", "who_knows", "Recorded, not sent", null],
  ])("labels a %s record (%s) in plain words", async (mode, reason, label, words) => {
    await activityWith([{ ...SIGNALS.signals[0], mode, reason, sent: false }]);

    const item = screen.getByTestId("activity-item-12");
    expect(within(item).getByTestId("activity-status").textContent).toBe(label);
    if (words) expect(item.textContent).toContain(words);
  });

  it("shows whether a first-look run was started, falling back to what it would have asked", async () => {
    await activityWith([
      { ...SIGNALS.signals[1], id: 21, mode: "delivered", sent: true, delivery_route: "page", triage: { started: true, reason: null } },
      { ...SIGNALS.signals[1], id: 22, mode: "delivered", sent: true, delivery_route: "page", triage: { started: false, reason: "no_engine" } },
      { ...SIGNALS.signals[1], id: 23, mode: "dry_run", sent: false },
    ]);

    expect(screen.getByTestId("activity-item-21").textContent).toContain("First-look run started");
    expect(screen.getByTestId("activity-item-22").textContent).toContain("No first-look run");
    expect(screen.getByTestId("activity-item-22").textContent).toContain("this organization's engine is not running");
    expect(screen.getByTestId("activity-item-23").textContent).toContain("Change nothing.");
  });

  it("never breaks on a record of an unknown shape", async () => {
    await activityWith([
      { id: 31, type: "alert", mode: 7, reason: { x: 1 }, triage: "yes", delivery_route: ["x"], surprise: true },
      { id: 32 },
      "not a record",
    ]);

    expect(screen.getByTestId("activity-item-31").textContent).toContain("Recorded, not sent");
    expect(screen.getByTestId("activity-item-32")).toBeTruthy();
  });

  it("says when nothing has been recorded", async () => {
    overrides = [[/\/api\/telemetry\/signals/, () => ({ body: { ...SIGNALS, signals: [] } })]];
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("activity");

    expect((await screen.findByTestId("activity-empty")).textContent).toMatch(/nothing recorded/i);
  });
});

describe("a query handed in by a link", () => {
  it("prefills the search and runs it", async () => {
    render(<LogsView role="admin" query={'"run_id=r1"'} />);

    await screen.findByTestId("telemetry-table");
    expect((screen.getByTestId("telemetry-query") as HTMLInputElement).value).toBe('"run_id=r1"');
    expect(new URL(sent(/\/api\/telemetry\/logs\?/)[0], "http://x").searchParams.get("q")).toBe('"run_id=r1"');
  });

  it("opens on the window the link carries, and shows it in the range picker", async () => {
    const since = "2026-11-24T08:55:00.000Z";
    render(<LogsView role="admin" query={'"run_id=r1"'} since={since} />);

    await screen.findByTestId("telemetry-table");
    const sent0 = new URL(sent(/\/api\/telemetry\/logs\?/)[0], "http://x");
    expect(sent0.searchParams.get("since")).toBe(since);
    expect(sent(/\/api\/telemetry\/logs\?/)).toHaveLength(1);
    const picker = screen.getByTestId("telemetry-since") as HTMLSelectElement;
    expect(picker.value).toBe(since);
    expect(picker.selectedOptions[0].textContent).toMatch(/^Since /);
  });

  it("switches to the Logs tab and runs a new query when the link changes", async () => {
    const { rerender } = render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    openTab("monitors");

    rerender(<LogsView role="admin" query={'"run_id=r2"'} />);

    expect(screen.getByTestId("telemetry-tab-logs").getAttribute("aria-selected")).toBe("true");
    await waitFor(() =>
      expect(new URL(sent(/\/api\/telemetry\/logs\?/).at(-1)!, "http://x").searchParams.get("q")).toBe('"run_id=r2"'),
    );
  });
});

describe("the organizations summary", () => {
  it("shows one row per organization for the root at home", async () => {
    render(<LogsView role="owner" rootAtHome hasChildTenants />);

    const summary = await screen.findByTestId("telemetry-orgs");
    await within(summary).findByText("Acme Shop");
    const shop = within(summary).getByTestId("telemetry-org-acme-child-a");
    expect(shop.textContent).toContain("5");
    expect(shop.textContent).toContain("3");
    expect(within(summary).getByTestId("telemetry-org-acme-child-b").textContent).toContain("Not connected");
    expect(within(summary).getByTestId("telemetry-org-acme-child-c").textContent).toMatch(/too many requests|unavailable/i);
    expect(sent(/\/api\/telemetry\/overview/)).toHaveLength(1);
  });

  it("still lands when the page went off screen while it was being read", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string) => {
      if (/\/api\/telemetry\/overview/.test(url)) {
        requests.push(url);
        await gate;
        return { ok: true, status: 200, json: async () => OVERVIEW } as Response;
      }
      return base(url);
    });

    const { rerender } = render(<LogsView role="owner" rootAtHome hasChildTenants />);
    await waitFor(() => expect(sent(/\/api\/telemetry\/overview/)).toHaveLength(1));
    rerender(<LogsView role="owner" rootAtHome hasChildTenants visible={false} />);
    release();
    await new Promise((resolve) => setTimeout(resolve, 0));
    rerender(<LogsView role="owner" rootAtHome hasChildTenants visible />);

    const summary = await screen.findByTestId("telemetry-orgs");
    await within(summary).findByTestId("telemetry-org-acme-child-a");
    expect(sent(/\/api\/telemetry\/overview/)).toHaveLength(1);
  });

  it("says which of an organization's two reads failed", async () => {
    overrides = [
      [
        /\/api\/telemetry\/overview/,
        () => ({
          body: {
            ...OVERVIEW,
            tenants: [
              { tenant_id: "acme-child-a", connected: true, errors: 5, firing_monitors: null, error_kind: "rate_limit" },
              { tenant_id: "acme-child-b", connected: true, errors: null, firing_monitors: 2, error_kind: "network" },
            ],
          },
        }),
      ],
    ];
    render(<LogsView role="owner" rootAtHome hasChildTenants />);

    const summary = await screen.findByTestId("telemetry-orgs");
    const shop = await within(summary).findByTestId("telemetry-org-acme-child-a");
    expect(within(shop).getByTestId("telemetry-org-errors").textContent).toBe("5");
    expect(within(shop).getByTestId("telemetry-org-monitors").textContent).toMatch(/unavailable/i);
    expect(within(shop).getByTestId("telemetry-org-monitors").getAttribute("title")).not.toMatch(/not shared/);
    const clinic = within(summary).getByTestId("telemetry-org-acme-child-b");
    expect(within(clinic).getByTestId("telemetry-org-errors").textContent).toMatch(/unavailable/i);
    expect(within(clinic).getByTestId("telemetry-org-monitors").textContent).toBe("2");
  });

  it("is not read again when the tab changes", async () => {
    render(<LogsView role="owner" rootAtHome hasChildTenants />);
    await screen.findByTestId("telemetry-orgs");

    openTab("monitors");
    await screen.findByTestId("monitors-table");
    openTab("logs");

    expect(screen.getByTestId("telemetry-orgs")).toBeTruthy();
    expect(sent(/\/api\/telemetry\/overview/)).toHaveLength(1);
  });

  it.each([
    ["a child's admin", { role: "admin", rootAtHome: false, hasChildTenants: false }],
    ["a root admin acting in a child", { role: "admin", rootAtHome: false, hasChildTenants: false }],
    ["a root auditor", { role: "auditor", rootAtHome: true, hasChildTenants: false }],
    ["a root without children", { role: "owner", rootAtHome: true, hasChildTenants: false }],
  ])("is not shown to %s", async (_who, props) => {
    render(<LogsView {...props} />);
    await screen.findByTestId("telemetry-table");

    expect(screen.queryByTestId("telemetry-orgs")).toBeNull();
    expect(sent(/\/api\/telemetry\/overview/)).toEqual([]);
  });
});
