/**
 * Observe › Logs, the organization's Datadog source, end to end, with every
 * bridge route intercepted.
 *
 * An admin of a child organization searches, sees rows and the errors-by-
 * service summary, and loads more with the bridge's cursor. The journald
 * "Server" source is the root operator's and is not offered. A member is not
 * offered the page at all (the bridge refuses them whatever the nav shows).
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const LOGS_URL = "/?v=logs";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function row(id: string, service: string, status: string, message: string) {
  return {
    id,
    timestamp: "2099-01-01T09:00:00Z",
    service,
    status,
    host: "web-1",
    message,
    tags: ["env:prod", `service:${service}`],
    attributes: { http: { status_code: 500 } },
  };
}

const FIRST_PAGE = {
  status: "ok",
  scope: "acme-child-a",
  rows: [
    {
      ...row("log-1", "shop-api", "error", "checkout failed"),
      attributes: { http: { status_code: 500 }, trace_id: "abc123def4567890" },
    },
    row("log-2", "shop-api", "info", "order placed"),
  ],
  next_cursor: "cursor-2",
};

const SECOND_PAGE = {
  status: "ok",
  scope: "acme-child-a",
  rows: [row("log-3", "shop-api", "warn", "slow checkout")],
  next_cursor: null,
};

const SUMMARY = {
  status: "ok",
  scope: "acme-child-a",
  group_by: "service",
  errors: true,
  buckets: [
    { by: "shop-api", count: 7 },
    { by: "clinic-app", count: 2 },
  ],
};

// A child organization's own session: not the root, not acting.
const CHILD_SESSION = {
  tenantId: "acme-child-a",
  homeTenantId: "acme-child-a",
  rootTenantId: "acme-parent",
};
// The root organization's operator, at home.
const ROOT_SESSION = {
  tenantId: "acme-parent",
  homeTenantId: "acme-parent",
  rootTenantId: "acme-parent",
};

async function setupShell(page: Page, role: string, session = CHILD_SESSION) {
  await page.route("**/api/auth/session", (route) =>
    json(route, {
      user: { name: "Alice", email: "agent@example.com" },
      role,
      ...session,
      expires: "2099-01-01T00:00:00.000Z",
    })
  );
  await page.route("**/api/chat/history", (route) => json(route, { messages: [] }));
  await page.route("**/api/chat/plan/status", (route) => json(route, { active: false, plan: null }));
  await page.route("**/api/chat/deep/status", (route) => json(route, { active: false, deep: null }));
  await page.route("**/api/session", (route) => json(route, {}));
  await page.route("**/api/health", (route) => json(route, { status: "ok", services: [] }));
  await page.route("**/api/events/stream*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: "event: ping\ndata: {}\n\n",
    })
  );
  await page.route("**/api/bridge/**", (route) =>
    json(route, { detail: "this route is not intercepted by the spec" }, 503)
  );
}

interface Seen {
  rows: string[];
  summaries: string[];
  journal: string[];
  traces: string[];
  monitors: string[];
  signals: string[];
  overview: string[];
}

async function setupTelemetry(page: Page, role = "admin", session = CHILD_SESSION): Promise<Seen> {
  const seen: Seen = {
    rows: [],
    summaries: [],
    journal: [],
    traces: [],
    monitors: [],
    signals: [],
    overview: [],
  };
  await setupShell(page, role, session);
  await page.route(/\/api\/bridge\/api\/telemetry\/logs\/summary\?/, (route) => {
    seen.summaries.push(route.request().url());
    return json(route, SUMMARY);
  });
  await page.route(/\/api\/bridge\/api\/telemetry\/logs\?/, (route) => {
    const url = route.request().url();
    seen.rows.push(url);
    return json(route, url.includes("cursor=") ? SECOND_PAGE : FIRST_PAGE);
  });
  await page.route(/\/api\/bridge\/api\/logs(\/units|\?)/, (route) => {
    seen.journal.push(route.request().url());
    return json(route, { detail: "operator role required" }, 403);
  });
  return seen;
}

const TRACE_ID = "abc123def4567890";
const RUN_ID = "11111111-1111-4111-8111-111111111111";

function span(spanId: string, parentId: string, resource: string, durationNs: number, start: string) {
  return {
    trace_id: TRACE_ID,
    span_id: spanId,
    parent_id: parentId,
    service: "shop-api",
    resource,
    operation: "http.request",
    timestamp: start,
    end: start,
    duration_ns: durationNs,
    status: spanId === "root-1" ? "error" : "ok",
    env: "prod",
    tags: [],
  };
}

const TRACES = {
  status: "ok",
  scope: "acme-child-a",
  rows: [span("root-1", "0", "POST /checkout", 9_000_000, "2099-01-01T09:00:00.000Z")],
  next_cursor: null,
};

const TRACE = {
  status: "ok",
  scope: "acme-child-a",
  trace_id: TRACE_ID,
  spans: [
    span("root-1", "0", "POST /checkout", 9_000_000, "2099-01-01T09:00:00.000Z"),
    span("child-1", "root-1", "SELECT orders", 2_500_000, "2099-01-01T09:00:00.001Z"),
  ],
};

const MONITORS = [
  { id: "1", name: "Checkout errors", status: "Alert", firing: true, type: "log alert", last_triggered: "2099-01-01T09:00:00+00:00" },
  { id: "2", name: "Cart latency", status: "OK", firing: false, type: "metric alert", last_triggered: null },
];

const SIGNALS = {
  status: "ok",
  scope: "acme-child-a",
  signals: [
    {
      id: 11,
      type: "alert",
      timestamp: "2099-01-01T09:00:05+00:00",
      mode: "dry_run",
      sent: false,
      recipients: "the instance operator, by telegram",
      title: "Checkout errors",
      transition: "triggered",
      priority: "P2",
      monitor_id: "1234",
      link: null,
      level: "critical",
      would_run: { agent: "main", prompt: "Take a read-only look. Change nothing." },
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
  ],
};

const ACCESSIBLE = [
  { id: "acme-parent", display_name: "Acme Holdings", parent_tenant_id: null, is_home: true },
  { id: "acme-child-a", display_name: "Acme Shop", parent_tenant_id: "acme-parent", is_home: false },
  { id: "acme-child-b", display_name: "Acme Clinic", parent_tenant_id: "acme-parent", is_home: false },
];

const RUN = {
  id: RUN_ID,
  agent_id: "example-agent",
  trigger_type: "cron",
  status: "failed",
  started_at: "2099-01-01T09:00:00+00:00",
  duration_ms: 42000,
};

/** The Traces, Monitors and Activity tabs, the root's overview, and a run to open. */
async function setupMoreTelemetry(page: Page, seen: Seen) {
  await page.route(/\/api\/bridge\/api\/telemetry\/traces\?/, (route) => {
    seen.traces.push(route.request().url());
    return json(route, TRACES);
  });
  await page.route(/\/api\/bridge\/api\/telemetry\/traces\/[0-9a-f]+/, (route) => {
    seen.traces.push(route.request().url());
    return json(route, TRACE);
  });
  await page.route(/\/api\/bridge\/api\/telemetry\/monitors/, (route) => {
    const url = route.request().url();
    seen.monitors.push(url);
    const firing = url.includes("firing=true");
    return json(route, {
      status: "ok",
      scope: "acme-child-a",
      firing,
      monitors: firing ? MONITORS.filter((m) => m.firing) : MONITORS,
    });
  });
  await page.route(/\/api\/bridge\/api\/telemetry\/signals/, (route) => {
    seen.signals.push(route.request().url());
    return json(route, SIGNALS);
  });
  await page.route(/\/api\/bridge\/api\/telemetry\/overview/, (route) => {
    seen.overview.push(route.request().url());
    return json(route, OVERVIEW);
  });
  await page.route("**/api/bridge/api/tenants/accessible", (route) => json(route, ACCESSIBLE));
  await page.route(/\/api\/bridge\/api\/runs\?/, (route) => json(route, [RUN]));
  await page.route(/\/api\/bridge\/api\/runs\/summary/, (route) =>
    json(route, {
      since_hours: 168,
      total: 1,
      by_status: { failed: 1 },
      by_delivery: { delivered: 0, none_expected: 1, failed: 0, pending: 0, not_recorded: 0 },
      by_agent: [{ agent_id: "example-agent", runs: 1, failed: 1, cost_usd: 0 }],
      total_cost_usd: 0,
    })
  );
  await page.route(/\/api\/bridge\/api\/runs\/1111[0-9a-f-]+$/, (route) =>
    json(route, { run: RUN, steps: [], guardrail_events: [] })
  );
}

test.describe("Observe › Logs › Datadog", () => {
  test("an admin searches, sees rows and errors by service, and loads more", async ({ page }) => {
    const seen = await setupTelemetry(page);
    await page.goto(LOGS_URL, { waitUntil: "networkidle" });

    await expect(page.getByTestId("logs-view")).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId("telemetry-table")).toContainText("checkout failed");
    await expect(page.getByTestId("telemetry-summary")).toContainText("Errors by service");
    await expect(page.getByTestId("telemetry-bucket-shop-api")).toContainText("7");
    // The journal is the root operator's: not offered, never asked.
    await expect(page.getByTestId("logs-source-server")).toHaveCount(0);

    const box = page.getByTestId("telemetry-query");
    await expect(box).toHaveAttribute("placeholder", /status:error service:shop-api/);
    await box.fill("status:error env:prod");
    await box.press("Enter");
    await expect
      .poll(() => new URL(seen.rows[seen.rows.length - 1] ?? "http://x").searchParams.get("q"))
      .toBe("status:error env:prod");
    await expect
      .poll(() =>
        new URL(seen.summaries[seen.summaries.length - 1] ?? "http://x").searchParams.get("q")
      )
      .toBe("status:error env:prod");

    await page.getByTestId("telemetry-more").click();
    await expect(page.getByTestId("telemetry-table")).toContainText("slow checkout");
    await expect(page.getByTestId("telemetry-table")).toContainText("checkout failed");
    expect(seen.rows[seen.rows.length - 1]).toContain("cursor=cursor-2");
    await expect(page.getByTestId("telemetry-more")).toHaveCount(0);

    await page.getByTestId("telemetry-row-0").click();
    await expect(page.getByTestId("telemetry-detail")).toContainText("status_code");

    expect(seen.journal).toEqual([]);
  });

  test("keeps the search and the summary usable on a 390 px screen", async ({ page }) => {
    await setupTelemetry(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(LOGS_URL, { waitUntil: "networkidle" });

    await expect(page.getByTestId("telemetry-query")).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId("telemetry-submit")).toBeVisible();
    await expect(page.getByTestId("telemetry-summary")).toBeVisible();

    // The table may scroll sideways inside its own frame; nothing above it may
    // push the page wider than the phone.
    const worst = await page.evaluate(() => {
      let overflow = 0;
      for (const id of ["telemetry-query", "telemetry-summary"]) {
        let el: Element | null = document.querySelector(`[data-testid="${id}"]`);
        while (el) {
          overflow = Math.max(overflow, el.scrollWidth - el.clientWidth);
          el = el.parentElement;
        }
      }
      return overflow;
    });
    expect(worst).toBeLessThanOrEqual(1);
  });

  test("opens a trace from a log line", async ({ page }) => {
    const seen = await setupTelemetry(page);
    await setupMoreTelemetry(page, seen);
    await page.goto(LOGS_URL, { waitUntil: "networkidle" });

    await page.getByTestId("telemetry-row-0").click();
    await page.getByTestId("telemetry-view-trace").click();

    await expect(page.getByTestId("telemetry-tab-traces")).toHaveAttribute("aria-selected", "true");
    const sheet = page.getByTestId("trace-detail");
    await expect(sheet.getByTestId("trace-span-root-1")).toContainText("POST /checkout");
    await expect(sheet.getByTestId("trace-span-child-1")).toHaveAttribute("data-depth", "1");
    expect(seen.traces.some((u) => u.includes(`/api/telemetry/traces/${TRACE_ID}`))).toBe(true);
  });

  test("the monitors tab shows firing monitors and every monitor on toggle", async ({ page }) => {
    const seen = await setupTelemetry(page);
    await setupMoreTelemetry(page, seen);
    await page.goto(LOGS_URL, { waitUntil: "networkidle" });

    await page.getByTestId("telemetry-tab-monitors").click();
    await expect(page.getByTestId("monitors-table")).toContainText("Checkout errors");
    await expect(page.getByTestId("monitors-table")).not.toContainText("Cart latency");
    expect(seen.monitors[0]).toContain("firing=true");

    await page.getByTestId("monitors-firing-toggle").click();
    await expect(page.getByTestId("monitors-table")).toContainText("Cart latency");
    expect(seen.monitors[seen.monitors.length - 1]).toContain("firing=false");
  });

  test("the activity tab lists a recorded alert as recorded, not sent", async ({ page }) => {
    const seen = await setupTelemetry(page);
    await setupMoreTelemetry(page, seen);
    await page.goto(LOGS_URL, { waitUntil: "networkidle" });

    await page.getByTestId("telemetry-tab-activity").click();
    const alert = page.getByTestId("activity-item-11");
    await expect(alert).toContainText("Checkout errors");
    await expect(alert).toContainText("Recorded, not sent");
    await expect(alert).toContainText("the instance operator, by telegram");
  });

  test("the run drawer's link lands on the Logs page filtered to the run", async ({ page }) => {
    const seen = await setupTelemetry(page);
    await setupMoreTelemetry(page, seen);
    await page.goto("/?v=runs", { waitUntil: "networkidle" });

    await page.getByTestId(`run-row-${RUN_ID}`).click();
    await page.getByTestId("run-detail-logs-link").click();

    await expect(page.getByTestId("telemetry-query")).toHaveValue(`"run_id=${RUN_ID}"`);
    await expect
      .poll(() => new URL(seen.rows[seen.rows.length - 1] ?? "http://x").searchParams.get("q"))
      .toBe(`"run_id=${RUN_ID}"`);
    expect(new URL(page.url()).searchParams.get("v")).toBe("logs");
    expect(new URL(page.url()).searchParams.get("q")).toBe(`"run_id=${RUN_ID}"`);
  });

  test("the organizations summary is shown to the root at home", async ({ page }) => {
    const seen = await setupTelemetry(page, "owner", ROOT_SESSION);
    await setupMoreTelemetry(page, seen);
    await page.goto(LOGS_URL, { waitUntil: "networkidle" });

    const orgs = page.getByTestId("telemetry-orgs");
    await expect(orgs).toBeVisible({ timeout: 15000 });
    await expect(orgs.getByTestId("telemetry-org-acme-child-a")).toContainText("Acme Shop");
    await expect(orgs.getByTestId("telemetry-org-acme-child-b")).toContainText("Not connected");
    expect(seen.overview).toHaveLength(1);
  });

  test("the organizations summary is not shown to a child's admin", async ({ page }) => {
    const seen = await setupTelemetry(page);
    await setupMoreTelemetry(page, seen);
    await page.goto(LOGS_URL, { waitUntil: "networkidle" });

    await expect(page.getByTestId("telemetry-table")).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId("telemetry-orgs")).toHaveCount(0);
    expect(seen.overview).toEqual([]);
  });

  test("a member is not offered the Logs page", async ({ page }) => {
    await setupShell(page, "member");
    await page.goto("/", { waitUntil: "networkidle" });

    await expect(page.getByTestId("nav-runs")).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId("nav-logs")).toHaveCount(0);
  });
});
