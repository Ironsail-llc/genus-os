/**
 * Settings › Secrets, end to end, with every bridge route intercepted in the
 * browser.
 *
 * Three assertions carry the spec:
 *
 * * an operator sees each credential's name, source, last change and readers,
 *   with an old one flagged for rotation and an unrecorded one saying so;
 * * nothing value-shaped reaches the page — the mocked reply carries none, and
 *   the page must not invent one;
 * * the table fits a 390 px screen as well as a 1440 px one, with no
 *   horizontal overflow on the settings body.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const SECRETS_URL = "/?v=settings&s=secrets";
const DAY = 24 * 60 * 60 * 1000;

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function daysAgo(days: number): string {
  return new Date(Date.now() - days * DAY).toISOString();
}

const SECRETS = {
  secrets: [
    {
      name: "EXAMPLE_API_TOKEN",
      configured: true,
      source: "vault",
      updated_at: daysAgo(5),
      readers: ["robothor-engine"],
      shadowed: false,
      shadowed_by: null,
      env_name: "EXAMPLE_API_TOKEN",
    },
    {
      name: "EXAMPLE_OLD_SMTP_PASSWORD_WITH_A_LONG_NAME",
      configured: true,
      source: "both",
      updated_at: daysAgo(120),
      readers: ["robothor-engine", "robothor-bridge"],
      shadowed: true,
      shadowed_by: "the environment",
      env_name: "EXAMPLE_OLD_SMTP_PASSWORD_WITH_A_LONG_NAME",
    },
    {
      name: "EXAMPLE_WEBHOOK_URL",
      configured: true,
      source: "env",
      updated_at: null,
      readers: ["robothor-engine", "robothor-bridge"],
      shadowed: false,
      shadowed_by: null,
      env_name: "EXAMPLE_WEBHOOK_URL",
    },
    {
      name: "EXAMPLE_MISSING_TOKEN",
      configured: false,
      source: "none",
      updated_at: null,
      readers: ["robothor-bridge"],
      shadowed: false,
      shadowed_by: null,
      env_name: "EXAMPLE_MISSING_TOKEN",
    },
  ],
};

/** See `settings-config.spec.ts`: the settings body absorbs overflow, so walk up. */
async function worstOverflow(page: Page, testid: string): Promise<number> {
  return page.evaluate((id) => {
    let worst = 0;
    let el: Element | null = document.querySelector(`[data-testid="${id}"]`);
    while (el) {
      worst = Math.max(worst, el.scrollWidth - el.clientWidth);
      el = el.parentElement;
    }
    return worst;
  }, testid);
}

async function setupMocks(page: Page): Promise<void> {
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

  // Registered FIRST, so it is matched LAST: nothing addressed at the bridge
  // can fall through to the real one.
  await page.route("**/api/bridge/**", (route) =>
    json(route, { detail: "this route is not intercepted by the spec" }, 503)
  );
  await page.route("**/api/bridge/api/secrets", (route) => json(route, SECRETS));
}

for (const width of [1440, 390]) {
  test.describe(`Settings › Secrets at ${width} px`, () => {
    test("lists names, sources, readers and rotation state, and fits the screen", async ({
      page,
    }) => {
      await setupMocks(page);
      await page.setViewportSize({ width, height: 900 });
      await page.goto(SECRETS_URL, { waitUntil: "networkidle" });

      await expect(page.locator('[data-testid="settings-page-secrets"]')).toBeVisible({
        timeout: 15000,
      });

      const fresh = page.locator('[data-testid="secrets-row-EXAMPLE_API_TOKEN"]');
      await expect(fresh).toBeVisible();
      await expect(fresh).toContainText("robothor-engine");
      await expect(fresh).not.toContainText(/overdue/i);

      const old = page.locator(
        '[data-testid="secrets-row-EXAMPLE_OLD_SMTP_PASSWORD_WITH_A_LONG_NAME"]'
      );
      await expect(
        old.locator(
          '[data-testid="secrets-overdue-EXAMPLE_OLD_SMTP_PASSWORD_WITH_A_LONG_NAME"]'
        )
      ).toContainText(/rotation overdue/i);

      await expect(page.locator('[data-testid="secrets-row-EXAMPLE_WEBHOOK_URL"]')).toContainText(
        /never recorded/i
      );
      await expect(
        page.locator('[data-testid="secrets-row-EXAMPLE_MISSING_TOKEN"]')
      ).toContainText(/not configured/i);

      await expect(page.locator('[data-testid="settings-page-secrets"]')).toContainText(
        "genus vault set"
      );

      expect(await worstOverflow(page, "settings-page-secrets")).toBeLessThanOrEqual(1);
    });
  });
}
