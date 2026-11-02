/**
 * Workflow runs and the run-detail drawer, end to end, against a bridge that
 * lives entirely in `page.route`. Nothing here reaches a real bridge.
 *
 * The flow: expand a workflow's runs, open the failed one, read the failing
 * step's error and its input and output — and at phone width, the drawer must
 * not push the page sideways however long a recorded payload is.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const AUTOMATIONS_URL = "/?v=workflows";
const WORKFLOW_ID = "example-pass";
const FAILED_RUN_ID = "22222222-2222-4222-8222-222222222222";

const RUN = {
  id: FAILED_RUN_ID,
  workflow_id: WORKFLOW_ID,
  status: "failed",
  trigger_type: "cron",
  steps_total: 2,
  steps_completed: 1,
  steps_failed: 1,
  steps_skipped: 0,
  duration_ms: 4200,
  started_at: "2026-06-15T11:00:00+00:00",
  completed_at: "2026-06-15T11:00:04.200000+00:00",
  error_message: "Step 'update_record' failed",
};

// One unbroken token, far wider than a phone: the payload that would widen
// the page if a <pre> were allowed to.
const LONG_TOKEN = "x".repeat(600);

const DETAIL = {
  ...RUN,
  trigger_detail: null,
  steps: [
    {
      step_id: "read_record",
      step_type: "tool",
      status: "completed",
      tool_name: "read_record",
      tool_input: { record_id: "rec-42" },
      tool_output: { id: "rec-42", note: LONG_TOKEN },
      error_message: null,
      duration_ms: 120,
    },
    {
      step_id: "update_record",
      step_type: "tool",
      status: "failed",
      tool_name: "update_record",
      tool_input: { record_id: "rec-42", state: "closed", note: LONG_TOKEN },
      tool_output: { error: "write refused: the record is read-only" },
      error_message: "write refused: the record is read-only",
      duration_ms: 300,
    },
  ],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

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
  await page.route("**/api/actions/execute", (route) =>
    json(route, { data: { agents: [] }, ok: true })
  );
  await page.route("**/api/events/stream*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: "event: ping\ndata: {}\n\n",
    })
  );
  await page.route("**/api/bridge/api/approvals", (route) => json(route, { count: 0, pending: [] }));
  await page.route("**/api/bridge/api/integrations/n8n/**", (route) =>
    json(route, {
      status: "not_configured",
      base_url: "",
      last_poll_at: null,
      last_ok_at: null,
      last_error: null,
      workflows: [],
    })
  );
  await page.route("**/api/bridge/api/automations", (route) =>
    json(route, { automations: [], count: 0 })
  );
  await page.route("**/api/bridge/api/workflows", (route) =>
    json(route, [
      {
        workflow_id: WORKFLOW_ID,
        runs: 1,
        last_run_at: RUN.started_at,
        last_status: "failed",
        failures: 1,
        source: "workspace",
      },
    ])
  );
  await page.route(`**/api/bridge/api/workflows/${WORKFLOW_ID}/runs`, (route) =>
    json(route, [RUN])
  );
  await page.route(`**/api/bridge/api/workflows/${WORKFLOW_ID}/runs/${FAILED_RUN_ID}`, (route) =>
    json(route, DETAIL)
  );
}

async function openFailedRun(page: Page) {
  await page.goto(AUTOMATIONS_URL, { waitUntil: "networkidle" });
  const toggle = page.locator(`[data-testid="workflow-runs-toggle-${WORKFLOW_ID}"]`);
  await expect(toggle).toBeVisible({ timeout: 15000 });
  await toggle.click();
  const run = page.locator(`[data-testid="workflow-run-${FAILED_RUN_ID}"]`);
  await expect(run).toContainText("1 ok, 1 failed");
  await run.click();
  const drawer = page.locator('[data-testid="workflow-run-drawer"]');
  await expect(drawer).toBeVisible();
  return drawer;
}

test.describe("Workflows › run detail", () => {
  test("a failed run shows the failing step's error, input and output", async ({ page }) => {
    await setupMocks(page);
    const drawer = await openFailedRun(page);

    await expect(drawer.locator('[data-testid="run-step-error-update_record"]')).toContainText(
      "write refused"
    );
    await expect(drawer.locator('[data-testid="run-step-input-update_record"] pre')).toBeVisible();
    await expect(drawer.locator('[data-testid="run-step-input-update_record"] pre')).toContainText(
      '"state": "closed"'
    );
    await expect(drawer.locator('[data-testid="run-step-output-update_record"] pre')).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();
  });

  test("the drawer does not overflow at phone width", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 780 });
    const drawer = await openFailedRun(page);

    // Open the succeeded step's output too, so its long token is laid out.
    await drawer.locator('[data-testid="run-step-output-read_record"] summary').click();
    await expect(drawer.locator('[data-testid="run-step-output-read_record"] pre')).toBeVisible();

    const metrics = await page.evaluate(() => ({
      docScroll: document.documentElement.scrollWidth,
      inner: window.innerWidth,
    }));
    expect(metrics.docScroll).toBeLessThanOrEqual(metrics.inner);

    const box = await drawer.boundingBox();
    expect(box).not.toBeNull();
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(391);

    // The drawer itself does not scroll sideways either: long values wrap.
    const drawerOverflow = await drawer.evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(drawerOverflow).toBeLessThanOrEqual(0);

    // Nor does a payload box: a long value wraps instead of hiding behind a
    // sideways scrollbar inside the <pre>.
    const preOverflow = await drawer
      .locator("pre")
      .evaluateAll((els) => Math.max(...els.map((el) => el.scrollWidth - el.clientWidth)));
    expect(preOverflow).toBeLessThanOrEqual(0);
  });
});
