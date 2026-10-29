/**
 * External: n8n, end to end, against a bridge that lives entirely in `page.route`.
 *
 * The section is read-only, so the claims are about what an operator sees: one
 * row per n8n workflow with its failure counts and a link out to n8n, the
 * section's status in its header, and a single muted line when monitoring is
 * switched off.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const AUTOMATIONS_URL = "/?v=workflows";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

const LISTING = {
  status: "warn",
  base_url: "https://n8n.example.com",
  last_poll_at: "2026-06-15T12:00:00Z",
  last_ok_at: "2026-06-15T12:00:00Z",
  last_error: null,
  workflows: [
    {
      workflow_id: "wf1",
      name: "Invoice sync",
      active: true,
      tags: ["billing"],
      failures_24h: 2,
      failures_7d: 5,
      last_failure_at: "2026-06-15T11:00:00Z",
      last_error: "timeout",
      url: "https://n8n.example.com/workflow/wf1",
    },
    {
      workflow_id: "wf2",
      name: "Lead intake",
      active: false,
      tags: [],
      failures_24h: 0,
      failures_7d: 0,
      last_failure_at: null,
      last_error: null,
      url: "https://n8n.example.com/workflow/wf2",
    },
  ],
};

async function setupMocks(page: Page, n8nBody: unknown): Promise<void> {
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
  await page.route("**/api/bridge/api/workflows", (route) => json(route, []));
  await page.route("**/api/bridge/api/automations", (route) =>
    json(route, { automations: [], count: 0 })
  );
  await page.route("**/api/bridge/api/integrations/n8n/**", (route) => json(route, n8nBody));
}

test.describe("Automations › External: n8n", () => {
  test("lists n8n workflows with failures and a link out", async ({ page }) => {
    await setupMocks(page, LISTING);
    await page.goto(AUTOMATIONS_URL, { waitUntil: "networkidle" });

    const section = page.locator('[data-testid="n8n-section"]');
    await expect(section).toBeVisible({ timeout: 15000 });
    await expect(section).toContainText("External: n8n");
    await expect(section.locator('[data-testid="n8n-status"]')).toContainText("Warn");

    const rows = section.locator('[data-testid="n8n-row"]');
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText("Invoice sync");
    await expect(rows.nth(0)).toContainText("2 / 5 failed");
    await expect(rows.nth(1)).toContainText("inactive");

    const link = rows.nth(0).getByRole("link");
    await expect(link).toHaveAttribute("href", "https://n8n.example.com/workflow/wf1");
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", /noopener/);
  });

  test("a switched-off monitor is one muted line", async ({ page }) => {
    await setupMocks(page, { ...LISTING, status: "not_configured", workflows: [] });
    await page.goto(AUTOMATIONS_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="n8n-not-configured"]')).toContainText(
      "n8n monitoring is not configured",
      { timeout: 15000 }
    );
    await expect(page.locator('[data-testid="n8n-row"]')).toHaveCount(0);
  });
});
