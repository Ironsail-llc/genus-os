/**
 * The command centre, end to end, with every backend intercepted in the browser.
 *
 * A parent tenant's admin opens "Command centre" from the nav, sees one card
 * per company with the one needing attention first and the unreadable one
 * saying so, and clicks a company to switch into it: the page reloads onto
 * that company under the "Acting as" banner. A child-tenant admin, whose
 * listing holds only its own tenant, gets no command centre at all. At phone
 * width the cards stack and nothing scrolls sideways. Each child company's card
 * carries a Connected block (its plugins, their checks and its remote tools);
 * against a bridge without that route the cards read exactly as before.
 *
 * Nothing here may reach a real bridge or engine.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

interface Tenant {
  id: string;
  display_name: string;
  parent_tenant_id: string | null;
  is_home: boolean;
}

const PARENT: Tenant = { id: "acme-parent", display_name: "Acme Holdings", parent_tenant_id: null, is_home: true };
const CHILD_A: Tenant = { id: "acme-child-a", display_name: "Acme Child A", parent_tenant_id: "acme-parent", is_home: false };
const CHILD_B: Tenant = { id: "acme-child-b", display_name: "Acme Child B", parent_tenant_id: "acme-parent", is_home: false };

const OVERVIEW = {
  as_of: "2026-11-18T12:00:00+00:00",
  home_tenant_id: "acme-parent",
  has_children: true,
  switching_enabled: true,
  tenants: [
    {
      tenant_id: "acme-parent",
      display_name: "Acme Holdings",
      parent_id: null,
      runs_24h: 4,
      failures_24h: 0,
      success_rate_24h: 1,
      cost_usd_24h: 0.1,
      runs_7d: 20,
      failures_7d: 0,
      success_rate_7d: 1,
      cost_usd_7d: 0.9,
      top_failures: [],
      agents_with_consecutive_errors: [],
      overdue_schedules: [],
      tasks: { open: 2, overdue: 0, oldest_overdue: [] },
      last_activity_at: "2026-11-18T11:00:00+00:00",
      status: "ok",
    },
    {
      tenant_id: "acme-child-a",
      display_name: "Acme Child A",
      parent_id: "acme-parent",
      runs_24h: 10,
      failures_24h: 2,
      success_rate_24h: 0.8,
      cost_usd_24h: 0.5,
      runs_7d: 70,
      failures_7d: 10,
      success_rate_7d: 0.8571,
      cost_usd_7d: 3.25,
      top_failures: [{ agent_id: "triage", error_type: "timeout", count: 2 }],
      agents_with_consecutive_errors: [{ agent_id: "triage", consecutive_errors: 3, last_status: "failed" }],
      overdue_schedules: [{ agent_id: "triage", next_run_at: "2026-11-18T09:00:00+00:00", overdue_minutes: 180 }],
      tasks: {
        open: 7,
        overdue: 1,
        oldest_overdue: [
          {
            id: "11111111-1111-1111-1111-111111111111",
            title: "Renew the lease",
            status: "TODO",
            assignedToAgent: "main",
            dueAt: "2026-11-13T12:00:00+00:00",
            overdueDays: 5,
            ageDays: 12,
          },
        ],
      },
      last_activity_at: "2026-11-18T11:55:00+00:00",
      status: "attention",
    },
    {
      tenant_id: "acme-child-b",
      display_name: "Acme Child B",
      parent_id: "acme-parent",
      status: "error",
      error: "could not read this tenant (OperationalError)",
    },
  ],
};

const CONNECTIONS = {
  as_of: "2026-11-18T12:00:00+00:00",
  home_tenant_id: "acme-parent",
  tenants: [
    {
      tenant_id: "acme-child-a",
      display_name: "Acme Child A",
      status: "ok",
      error: null,
      plugins: [{ name: "example-plugin", version: "1.2.3", enabled: true }],
      tools: [
        { name: "example_read", plugin: "example-plugin", access: "read", attestations: [], description: "Reads." },
        { name: "example_write", plugin: "example-plugin", access: "write", attestations: [], description: "Writes." },
      ],
      checks: [
        { name: "example-plugin.connection", status: "ok", detail: "" },
        { name: "example-plugin.token", status: "fail", detail: "expired" },
      ],
    },
    {
      tenant_id: "acme-child-b",
      display_name: "Acme Child B",
      status: "error",
      error: "the company's engine did not answer",
      plugins: [],
      tools: [],
      checks: [],
    },
  ],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function setupMocks(
  page: Page,
  identity: { home: string; accessible: Tenant[] },
  options: { connections?: boolean } = {},
) {
  const state = { acting: identity.home, switches: [] as string[], overviewReads: 0 };

  await page.route("**/api/auth/session", (route) =>
    json(route, {
      user: { name: "Alice", email: "alice@example.com" },
      role: "admin",
      tenantId: state.acting,
      homeTenantId: identity.home,
      backendAuthorized: true,
      expires: "2099-01-01T00:00:00.000Z",
    }),
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
    }),
  );
  await page.route("**/api/actions/execute", (route) => {
    const body = route.request().postDataJSON() as { tool?: string } | null;
    return json(route, { data: body?.tool === "agent_status" ? { agents: [] } : { tasks: [] } });
  });

  // Registered FIRST, so it is matched LAST: nothing addressed at the bridge
  // falls through to a real one.
  await page.route("**/api/bridge/**", (route) =>
    json(route, { detail: "this route is not intercepted by the spec" }, 503),
  );
  await page.route("**/api/bridge/api/tenants/accessible", (route) => json(route, identity.accessible));
  await page.route("**/api/bridge/api/org/overview", (route) => {
    state.overviewReads += 1;
    return state.acting === identity.home
      ? json(route, OVERVIEW)
      : json(route, { detail: "switch back to your home tenant to see every company" }, 403);
  });
  // An older bridge has no connections route: it answers 404.
  await page.route("**/api/bridge/api/org/connections", (route) =>
    options.connections === false ? json(route, { detail: "Not Found" }, 404) : json(route, CONNECTIONS),
  );
  await page.route("**/api/session/switch-tenant", (route) => {
    const { tenant } = route.request().postDataJSON() as { tenant: string };
    if (!identity.accessible.some((t) => t.id === tenant)) {
      return json(route, { error: "tenant not accessible" }, 403);
    }
    state.switches.push(tenant);
    state.acting = tenant;
    return json(route, { tenant, homeTenant: identity.home });
  });

  return state;
}

test.describe("command centre", () => {
  test("a parent admin reads every company and opens one", async ({ page }) => {
    const state = await setupMocks(page, { home: "acme-parent", accessible: [PARENT, CHILD_A, CHILD_B] });

    await page.goto("/?v=tasks");
    await page.getByTestId("nav-org").click();
    await expect(page).toHaveURL(/\?v=org/);

    const cards = page.getByTestId(/^company-card-/);
    await expect(cards).toHaveCount(3);
    // Attention first, then the unreadable one, then the healthy parent.
    await expect(cards.nth(0)).toHaveAttribute("data-testid", "company-card-acme-child-a");
    await expect(cards.nth(1)).toHaveAttribute("data-testid", "company-card-acme-child-b");
    await expect(cards.nth(2)).toHaveAttribute("data-testid", "company-card-acme-parent");

    const childA = page.getByTestId("company-card-acme-child-a");
    await expect(childA).toContainText("Needs attention");
    await expect(childA).toContainText("Renew the lease");
    await expect(childA).toContainText("5 days overdue");
    await expect(childA.getByTestId("top-failures")).toContainText("timeout");
    await expect(page.getByTestId("company-card-acme-child-b")).toContainText("could not read this tenant");
    await expect(page.getByTestId("company-card-acme-child-b").getByTestId("runs-24h")).toHaveCount(0);
    await expect(page.getByTestId("org-as-of")).toBeVisible();

    // What each child company has connected.
    const connected = childA.getByTestId("connected");
    await expect(connected.getByRole("heading", { name: "Connected" })).toBeVisible();
    await expect(connected.getByTestId("connected-plugins")).toContainText("example-plugin");
    await expect(connected.getByTestId("connected-plugins")).toContainText("1.2.3");
    await expect(connected.getByTestId("connected-checks")).toContainText("1 fail");
    await expect(connected.getByTestId("connected-failing")).toContainText("example-plugin.token");
    await expect(connected.getByTestId("connected-tools")).toContainText("1 read");
    await expect(connected.getByTestId("connected-tools")).toContainText("1 write");
    await expect(page.getByTestId("company-card-acme-child-b").getByTestId("connected-error")).toContainText(
      "could not be read",
    );

    await page.screenshot({ path: test.info().outputPath("command-centre.png"), fullPage: true });

    await page.getByRole("button", { name: "Open Acme Child A" }).click();

    await expect(page.getByTestId("acting-tenant-banner")).toContainText("Acting as Acme Child A");
    expect(state.switches).toEqual(["acme-child-a"]);
    // While acting, the command centre is not offered.
    await expect(page.getByTestId("nav-org")).toHaveCount(0);
  });

  // Each read is audited in every child. Not re-reading on tab focus is
  // covered in the unit test, which can move the clock past the minute.
  test("the overview is read once on opening, and once more per Refresh", async ({ page }) => {
    const state = await setupMocks(page, { home: "acme-parent", accessible: [PARENT, CHILD_A, CHILD_B] });

    await page.goto("/?v=org");
    await expect(page.getByTestId(/^company-card-/)).toHaveCount(3);
    expect(state.overviewReads).toBe(1);

    const refresh = page.getByRole("button", { name: "Refresh" });
    await refresh.click();
    await expect.poll(() => state.overviewReads).toBe(2);
    await expect(refresh).toBeEnabled();
    expect(state.overviewReads).toBe(2);
  });

  test("against a bridge without the connections route, the cards read as before", async ({ page }) => {
    await setupMocks(page, { home: "acme-parent", accessible: [PARENT, CHILD_A, CHILD_B] }, { connections: false });

    await page.goto("/?v=org");

    await expect(page.getByTestId(/^company-card-/)).toHaveCount(3);
    await expect(page.getByTestId("company-card-acme-child-a")).toContainText("Renew the lease");
    await expect(page.getByTestId("connected")).toHaveCount(0);
    await expect(page.getByTestId("org-error")).toHaveCount(0);
  });

  test("a child-tenant admin has no command centre", async ({ page }) => {
    await setupMocks(page, { home: "acme-child-a", accessible: [{ ...CHILD_A, is_home: true }] });

    await page.goto("/?v=tasks");
    await expect(page.getByTestId("header-title")).toHaveText("Tasks");

    await expect(page.getByTestId("nav-org")).toHaveCount(0);
  });

  test("the cards fit a phone", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await setupMocks(page, { home: "acme-parent", accessible: [PARENT, CHILD_A, CHILD_B] });

    await page.goto("/?v=org");

    await expect(page.getByTestId(/^company-card-/)).toHaveCount(3);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);

    await page.screenshot({ path: test.info().outputPath("command-centre-phone.png"), fullPage: true });
  });
});
