/**
 * Observe › Runs, end to end, with every bridge route intercepted.
 *
 * What matters here is layout and the one interaction the unit tests cannot
 * see: the full-width table must never push the page sideways (at a desktop
 * width, at 1024 px with the chat dock open, and on a phone), and selecting a
 * row must open the run in a drawer that fits the screen it opened on.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const RUNS_URL = "/?v=runs";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

const RUN = {
  id: "11111111-1111-4111-8111-111111111111",
  agent_id: "example-agent",
  trigger_type: "cron",
  status: "completed",
  started_at: "2099-01-01T09:00:00+00:00",
  duration_ms: 42000,
  model_used: "example-provider/example-model-with-a-long-name",
  input_tokens: 12000,
  output_tokens: 3000,
  total_cost_usd: 0.0123,
  delivery_status: "suppressed_trivial",
  verified_status: "verified",
};

const DETAIL = {
  run: {
    ...RUN,
    trigger_detail: "schedule:daily-digest",
    task_text: "Summarise the overnight inbox for Alice and note anything from Bob.",
    outcome_assessment: "successful",
    outcome_notes: "Read every inbox.",
    models_attempted: ["example-provider/example-model-with-a-long-name"],
    cache_read_tokens: 9000,
  },
  steps: [
    { step_number: 0, step_type: "warmup_phase", tool_name: "memory_blocks", duration_ms: 12 },
    {
      step_number: 1,
      step_type: "tool_call",
      tool_name: "read_inbox",
      duration_ms: 230,
      tool_input: { folder: "inbox", query: "x".repeat(400) },
      tool_output: { _truncated: true, total_chars: 9000, content: "y".repeat(3900) },
    },
  ],
  guardrail_events: [],
};

async function setupMocks(page: Page) {
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
  // Regexes, not globs: `?` is a wildcard in a Playwright glob.
  await page.route(/\/api\/bridge\/api\/runs\?/, (route) => json(route, [RUN]));
  await page.route(/\/api\/bridge\/api\/runs\/summary/, (route) =>
    json(route, {
      since_hours: 168,
      total: 1,
      by_status: { completed: 1 },
      by_delivery: { delivered: 0, none_expected: 1, failed: 0, pending: 0, not_recorded: 0 },
      by_agent: [{ agent_id: "example-agent", runs: 1, failed: 0, cost_usd: 0.0123 }],
      total_cost_usd: 0.0123,
    })
  );
  await page.route(/\/api\/bridge\/api\/runs\/1111[0-9a-f-]+$/, (route) => json(route, DETAIL));
}

/** The worst horizontal overflow from the element up to the document. */
async function worstOverflow(page: Page, selector: string): Promise<number> {
  return page.evaluate((sel) => {
    let overflow = 0;
    let el: Element | null = document.querySelector(sel);
    while (el) {
      overflow = Math.max(overflow, el.scrollWidth - el.clientWidth);
      el = el.parentElement;
    }
    return overflow;
  }, selector);
}

async function openRuns(page: Page) {
  await page.goto(RUNS_URL, { waitUntil: "networkidle" });
  await expect(page.locator('[data-testid="runs-view"]')).toBeVisible({ timeout: 15000 });
  await expect(page.locator(`[data-testid="run-row-${RUN.id}"]`)).toBeVisible();
}

async function openDrawer(page: Page) {
  await page.locator(`[data-testid="run-row-${RUN.id}"]`).click();
  await expect(page.locator('[data-testid="run-detail"]')).toBeVisible();
  await expect(page.locator('[data-testid="run-detail-task"]')).toContainText("overnight inbox");
  await page.locator('[data-testid="step-io-toggle-1"]').click();
  await expect(page.locator('[data-testid="step-truncated-1"]')).toContainText("4,000");
  const box = await page.locator('[data-testid="run-detail-drawer"]').boundingBox();
  const width = page.viewportSize()!.width;
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(-1);
  expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
  expect(await worstOverflow(page, '[data-testid="run-detail"]')).toBeLessThanOrEqual(1);
}

test.describe("Observe › Runs", () => {
  test("a full-width table that does not overflow at 1440 px, and a drawer", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await openRuns(page);
    await expect(page.locator(`[data-testid="run-delivered-${RUN.id}"]`)).toContainText(
      "no delivery expected"
    );
    const table = await page.locator('[data-testid="runs-table"]').boundingBox();
    const view = await page.locator('[data-testid="runs-view"]').boundingBox();
    // Full width: the table spans the view, less its padding.
    expect(table!.width).toBeGreaterThan(view!.width - 48);
    expect(await worstOverflow(page, '[data-testid="runs-view"]')).toBeLessThanOrEqual(1);
    await openDrawer(page);
  });

  test("no horizontal overflow at 1024 px with the chat dock open", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 1024, height: 768 });
    await openRuns(page);
    const openDock = page.locator('[data-testid="chat-dock-open"]');
    if (await openDock.isVisible()) await openDock.click();
    // The dock is open: its close control is on screen.
    await expect(page.locator('[data-testid="chat-dock-close"]')).toBeVisible();
    expect(await worstOverflow(page, '[data-testid="runs-view"]')).toBeLessThanOrEqual(1);
  });

  test("no horizontal overflow at 390 px, and the drawer fits the phone", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await openRuns(page);
    await expect(page.locator('[data-testid="runs-filter-period"]')).toBeVisible();
    expect(await worstOverflow(page, '[data-testid="runs-view"]')).toBeLessThanOrEqual(1);
    await openDrawer(page);
  });
});
