/**
 * Observe › Memory, end to end, with every bridge route intercepted — this
 * spec must never reach a real bridge, because the act under test bounds a
 * fact on a live instance's memory.
 *
 * The two assertions that matter: the forget is PREVIEWED before it happens
 * and the preview names the always-in-context blocks that quote the text (an
 * agent carrying one keeps repeating the fact after it is forgotten, which is
 * the single thing an operator most needs to know before pressing the button),
 * and the row afterwards is the row the SERVER returned — not a row this page
 * decided would be right.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const MEMORY_URL = "/?v=memory";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

const ACTIVE_FACT = {
  id: 4711,
  fact_text: "Alice prefers tea",
  entities: ["Alice"],
  is_active: true,
  valid_from: null,
  valid_to: null,
  confidence: 0.92,
  source: "chat",
  created_at: "2099-01-01T09:00:00+00:00",
  superseded_by: null,
};

const OTHER_FACT = {
  ...ACTIVE_FACT,
  id: 4710,
  fact_text: "Bob works at Example Corp",
  entities: ["Bob", "Example Corp"],
};

interface Recorded {
  previews: string[];
  forgets: Array<{ url: string; body: Record<string, unknown> }>;
}

async function setupMocks(page: Page): Promise<Recorded> {
  const recorded: Recorded = { previews: [], forgets: [] };

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

  // Registered FIRST so it matches LAST: nothing addressed at the bridge can
  // fall through to a real one.
  await page.route("**/api/bridge/**", (route) =>
    json(route, { detail: "this route is not intercepted by the spec" }, 503)
  );

  await page.route(/\/api\/bridge\/api\/memory\/facts\/\d+\/forget\/preview$/, (route) => {
    recorded.previews.push(route.request().url());
    return json(route, {
      fact: ACTIVE_FACT,
      would_deactivate: [4711],
      references: {
        entities: ["Alice"],
        episodes: 3,
        blocks: ["working_context"],
        blocks_scanned: true,
      },
      already_inactive: false,
    });
  });

  await page.route(/\/api\/bridge\/api\/memory\/facts\/\d+\/forget$/, (route) => {
    recorded.forgets.push({
      url: route.request().url(),
      body: (route.request().postDataJSON() ?? {}) as Record<string, unknown>,
    });
    return json(route, {
      fact: { ...ACTIVE_FACT, is_active: false, valid_to: "2099-01-01T10:00:00+00:00" },
      forgotten: true,
    });
  });

  await page.route(/\/api\/bridge\/api\/memory\/facts\?/, (route) =>
    json(route, {
      facts: [
        recorded.forgets.length
          ? { ...ACTIVE_FACT, is_active: false, valid_to: "2099-01-01T10:00:00+00:00" }
          : ACTIVE_FACT,
        OTHER_FACT,
      ],
      next_cursor: null,
    })
  );

  return recorded;
}

test.describe("Observe › Memory", () => {
  test("previews a forget, warns about the blocks, and bounds the fact", async ({ page }) => {
    const recorded = await setupMocks(page);
    await page.goto(MEMORY_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="memory-view"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="memory-fact-4711"]')).toContainText(
      "Alice prefers tea"
    );

    await page.locator('[data-testid="memory-forget-4711"]').click();

    const card = page.locator('[data-testid="memory-confirm-4711"]');
    await expect(card).toBeVisible();
    await expect(page.locator('[data-testid="memory-blocks-warning-4711"]')).toContainText(
      "working_context"
    );
    // The preview writes nothing, and nothing has been forgotten yet.
    expect(recorded.previews).toHaveLength(1);
    expect(recorded.forgets).toHaveLength(0);

    // No reason, no forget.
    await expect(page.locator('[data-testid="memory-confirm-go-4711"]')).toBeDisabled();

    await page
      .locator('[data-testid="memory-reason-4711"]')
      .fill("she asked for this to be dropped");
    await page.locator('[data-testid="memory-confirm-go-4711"]').click();

    await expect(page.locator('[data-testid="memory-inactive-4711"]')).toBeVisible();
    await expect(card).toHaveCount(0);
    // One forget bounds one row.
    await expect(page.locator('[data-testid="memory-inactive-4710"]')).toHaveCount(0);

    expect(recorded.forgets).toHaveLength(1);
    expect(recorded.forgets[0].body).toEqual({ reason: "she asked for this to be dropped" });
  });

  test("keeps the whole forget flow usable on a 390 px screen", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(MEMORY_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="memory-view"]')).toBeVisible({ timeout: 15000 });
    await page.locator('[data-testid="memory-forget-4711"]').click();
    await expect(page.locator('[data-testid="memory-reason-4711"]')).toBeVisible();
    await expect(page.locator('[data-testid="memory-confirm-go-4711"]')).toBeVisible();

    const worst = await page.evaluate(() => {
      let overflow = 0;
      let el: Element | null = document.querySelector('[data-testid="memory-view"]');
      while (el) {
        overflow = Math.max(overflow, el.scrollWidth - el.clientWidth);
        el = el.parentElement;
      }
      return overflow;
    });
    expect(worst).toBeLessThanOrEqual(1);
  });
});
