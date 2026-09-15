/**
 * Observe › Logs, end to end, with every bridge route intercepted.
 *
 * Two states, because the second one is what most deployments of this app
 * actually see: a box with journald, and a container without it. The second
 * must render as an honest empty state carrying the server's own reason and
 * must not be red — `available: false` is a 200, and reporting an appliance
 * fault where there is none sends an operator hunting a problem that does not
 * exist.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const LOGS_URL = "/?v=logs";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

const UNITS = {
  units: [
    { name: "robothor-engine", description: "Genus OS Agent Engine (Python)" },
    { name: "robothor-bridge", description: "Genus OS CRM bridge" },
  ],
  available: true,
};

const LINES = {
  unit: "robothor-engine",
  available: true,
  lines: [
    { ts: "2099-01-01T09:00:00+00:00", priority: 6, message: "engine started" },
    { ts: "2099-01-01T09:00:01+00:00", priority: 4, message: "provider slow, retrying" },
    { ts: "2099-01-01T09:00:02+00:00", priority: 3, message: "run failed: timeout" },
  ],
  truncated: true,
};

async function setupShell(page: Page) {
  await page.route("**/api/auth/session", (route) =>
    json(route, {
      user: { name: "Operator", email: "operator@example.com" },
      role: "owner",
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

async function setupJournald(page: Page): Promise<string[]> {
  const reads: string[] = [];
  await setupShell(page);
  await page.route(/\/api\/bridge\/api\/logs\/units/, (route) => json(route, UNITS));
  await page.route(/\/api\/bridge\/api\/logs\?/, (route) => {
    reads.push(route.request().url());
    return json(route, LINES);
  });
  return reads;
}

test.describe("Observe › Logs", () => {
  test("reads a unit, badges by priority, and filters", async ({ page }) => {
    const reads = await setupJournald(page);
    await page.goto(LOGS_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="logs-view"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="logs-pane"]')).toContainText("run failed: timeout");
    await expect(page.locator('[data-testid="logs-priority-2"]')).toHaveAttribute(
      "data-level",
      "error"
    );
    await expect(page.locator('[data-testid="logs-priority-1"]')).toHaveAttribute(
      "data-level",
      "warning"
    );
    await expect(page.locator('[data-testid="logs-truncated"]')).toBeVisible();

    await page.locator('[data-testid="logs-since-1h"]').click();
    await expect.poll(() => reads[reads.length - 1] ?? "").toContain("since=1h");

    await page.locator('[data-testid="logs-grep"]').fill("timeout");
    await page.locator('[data-testid="logs-refresh"]').click();
    await expect.poll(() => reads[reads.length - 1] ?? "").toContain("grep=timeout");

    // journald's English is refused here rather than at the bridge, so the
    // operator sees it under the field they typed it into.
    await page.locator('[data-testid="logs-since-custom"]').fill("2 hours ago");
    await page.locator('[data-testid="logs-refresh"]').click();
    await expect(page.locator('[data-testid="logs-since-error"]')).toBeVisible();

    // Auto-refresh is off until it is asked for.
    await expect(page.locator('[data-testid="logs-auto"]')).toHaveAttribute(
      "aria-pressed",
      "false"
    );
  });

  test("renders a container with no journald as a state, not a failure", async ({ page }) => {
    await setupShell(page);
    const reason = "journald is not available on this deployment (journalctl is not installed)";
    await page.route(/\/api\/bridge\/api\/logs\/units/, (route) =>
      json(route, { units: [], available: false, reason })
    );

    await page.goto(LOGS_URL, { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="logs-view"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="logs-unavailable"]')).toContainText(reason);
    await expect(page.locator('[data-testid="logs-error"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="logs-pane"]')).toHaveCount(0);
  });

  test("keeps the controls and the pane usable on a 390 px screen", async ({ page }) => {
    await setupJournald(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(LOGS_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="logs-view"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="logs-unit"]')).toBeVisible();
    await expect(page.locator('[data-testid="logs-refresh"]')).toBeVisible();

    // The pane itself may scroll sideways — a log line is as long as it is —
    // but nothing above it may push the page wider than the phone.
    const worst = await page.evaluate(() => {
      let overflow = 0;
      let el: Element | null = document.querySelector('[data-testid="logs-unit"]');
      while (el) {
        if (!(el as HTMLElement).dataset?.testid?.startsWith("logs-pane")) {
          overflow = Math.max(overflow, el.scrollWidth - el.clientWidth);
        }
        el = el.parentElement;
      }
      return overflow;
    });
    expect(worst).toBeLessThanOrEqual(1);
  });
});
