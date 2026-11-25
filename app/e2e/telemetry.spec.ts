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
    row("log-1", "shop-api", "error", "checkout failed"),
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

async function setupShell(page: Page, role: string) {
  await page.route("**/api/auth/session", (route) =>
    json(route, {
      user: { name: "Alice", email: "agent@example.com" },
      role,
      // A child organization's own session: not the root, not acting.
      tenantId: "acme-child-a",
      homeTenantId: "acme-child-a",
      rootTenantId: "acme-parent",
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
}

async function setupTelemetry(page: Page): Promise<Seen> {
  const seen: Seen = { rows: [], summaries: [], journal: [] };
  await setupShell(page, "admin");
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

  test("a member is not offered the Logs page", async ({ page }) => {
    await setupShell(page, "member");
    await page.goto("/", { waitUntil: "networkidle" });

    await expect(page.getByTestId("nav-runs")).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId("nav-logs")).toHaveCount(0);
  });
});
