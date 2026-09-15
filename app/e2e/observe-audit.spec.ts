/**
 * Observe › Audit, end to end, with every bridge route intercepted.
 *
 * The assertions that matter: the filters an operator typed reach BOTH the
 * table and the export link (an export that quietly ignored them is worse than
 * no export), the export is a real download rather than a fetch-and-blob, and
 * the two tabs read two different routes — the second one only when it is
 * opened.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const AUDIT_URL = "/?v=audit";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

const EVENT = {
  id: 91,
  timestamp: "2099-01-01T09:00:00+00:00",
  event_type: "memory.forget",
  category: "memory",
  actor: "operator:alice",
  action: "4711",
  target: "memory_facts",
  status: "success",
  source_channel: "helm",
  session_key: "sess-1",
  user_id: "u-1",
  details: '{"fact_id": 4711}',
};

interface Recorded {
  events: string[];
  changes: string[];
  exports: string[];
}

async function setupMocks(page: Page): Promise<Recorded> {
  const recorded: Recorded = { events: [], changes: [], exports: [] };

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

  /*
    Regexes, not globs, for the bridge routes below. In a Playwright URL glob
    `?` is a single-character wildcard, so `…/audit/events?**` also matches
    `…/audit/events.csv?…` — and because a later `page.route` wins, the events
    listing swallowed the export and the download came back named
    `events.json`. The three paths here differ only in punctuation, which is
    exactly the case a glob cannot be trusted with.
  */
  // The CSV route, served the way the bridge serves it — an attachment.
  await page.route(/\/api\/bridge\/api\/audit\/events\.csv/, (route) => {
    recorded.exports.push(route.request().url());
    return route.fulfill({
      status: 200,
      contentType: "text/csv; charset=utf-8",
      headers: { "content-disposition": 'attachment; filename="audit-acme-2099-01-01.csv"' },
      body: "id,timestamp,event_type\r\n91,2099-01-01T09:00:00+00:00,memory.forget\r\n",
    });
  });

  await page.route(/\/api\/bridge\/api\/audit\/events\?/, (route) => {
    recorded.events.push(route.request().url());
    return json(route, { events: [EVENT], count: 1 });
  });

  await page.route(/\/api\/bridge\/api\/controls\/audit\?/, (route) => {
    recorded.changes.push(route.request().url());
    return json(route, {
      changes: [
        {
          id: 7,
          flag: "ROBOTHOR_RBAC_MODE",
          old_value: "observe",
          new_value: "enforce",
          changed_by: "operator:alice",
          reason: "soak is clean",
          changed_at: "2099-01-01T09:00:00+00:00",
        },
      ],
      next_cursor: null,
    });
  });

  return recorded;
}

test.describe("Observe › Audit", () => {
  test("filters the table and the export together, and downloads a file", async ({ page }) => {
    const recorded = await setupMocks(page);
    await page.goto(AUDIT_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="audit-view"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="audit-event-91"]')).toContainText("memory.forget");
    // The other tab's route is not read until the other tab is opened.
    expect(recorded.changes).toHaveLength(0);

    await page.locator('[data-testid="audit-filter-event-type"]').fill("memory.forget");
    await page.locator('[data-testid="audit-filter-since"]').fill("2099-01-01");
    await page.locator('[data-testid="audit-apply"]').click();

    await expect
      .poll(() => recorded.events[recorded.events.length - 1] ?? "")
      .toContain("event_type=memory.forget");
    expect(recorded.events[recorded.events.length - 1]).toContain("since=2099-01-01");

    const link = page.locator('[data-testid="audit-export"]');
    await expect(link).toHaveAttribute("href", /event_type=memory.forget/);
    await expect(link).toHaveAttribute("href", /since=2099-01-01/);

    await expect(link).toHaveAttribute("download", "");

    /*
      The link is FETCHED, not clicked.

      Playwright's route interception does not apply to a download request — a
      `page.route` handler is never called for one, checked and not assumed —
      so clicking this anchor sends a request that leaves the fixture entirely.
      It used to reach `localhost:9100`, the live bridge on a developer box,
      with the Helm's own dev credentials attached; `BRIDGE_URL` is now pinned
      to a dead port in `playwright.config.ts` so that can no longer happen,
      and the click is gone as well.

      Fetching the anchor's own `href` proves what the click would have proved
      and can actually be observed: that the href is a live, correctly
      addressed URL carrying the operator's filters, and that what comes back
      down it is a CSV attachment. That the app's proxy carries the bridge's
      `Content-Disposition` and `Content-Type` across — the part this
      browser-level interception steps in front of — is pinned in
      `__tests__/api/bridge-proxy.test.ts`, where the upstream reply can be
      controlled.
    */
    const reply = await page.evaluate(async () => {
      const href = document.querySelector<HTMLAnchorElement>('[data-testid="audit-export"]')!.href;
      const res = await fetch(href);
      return {
        type: res.headers.get("content-type"),
        disposition: res.headers.get("content-disposition"),
        body: await res.text(),
      };
    });
    expect(reply.type).toContain("text/csv");
    expect(reply.disposition).toContain("audit-acme-2099-01-01.csv");
    expect(reply.body).toContain("memory.forget");
    expect(recorded.exports).toHaveLength(1);
    expect(recorded.exports[0]).toContain("event_type=memory.forget");
    expect(recorded.exports[0]).toContain("since=2099-01-01");

    await page.locator('[data-testid="audit-tab-flags"]').click();
    await expect(page.locator('[data-testid="audit-old-7"]')).toHaveText("observe");
    await expect(page.locator('[data-testid="audit-new-7"]')).toHaveText("enforce");
    expect(recorded.changes.length).toBeGreaterThan(0);
  });

  test("keeps both tabs usable on a 390 px screen", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(AUDIT_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="audit-view"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="audit-export"]')).toBeVisible();
    await page.locator('[data-testid="audit-tab-flags"]').click();
    await expect(page.locator('[data-testid="audit-change-7"]')).toBeVisible();

    const worst = await page.evaluate(() => {
      let overflow = 0;
      let el: Element | null = document.querySelector('[data-testid="audit-view"]');
      while (el) {
        overflow = Math.max(overflow, el.scrollWidth - el.clientWidth);
        el = el.parentElement;
      }
      return overflow;
    });
    expect(worst).toBeLessThanOrEqual(1);
  });
});
