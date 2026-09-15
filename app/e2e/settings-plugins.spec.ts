/**
 * Settings › Plugins, end to end, with every bridge route intercepted in the
 * browser — this spec must never reach a real bridge, because two of the three
 * acts under test write a governance file on a live instance and the third
 * re-imports every plugin the engine has.
 *
 * The flow is the one a fresh box actually goes through, in order:
 *
 *   1. nothing recorded — the lockfile does not exist, every switch is dead,
 *      and Record is the only thing on the page that works;
 *   2. record — one row per installed distribution, and the switches come alive;
 *   3. disable one — the row is written, the STATE pill does not move (the
 *      engine is still running it), and a bar comes up saying exactly that;
 *   4. reload — and the plugin the operator turned off comes back in the
 *      failures list as `disabled by operator`, which is their own decision
 *      arriving back at them and is not rendered as a fault.
 *
 * Step 3 is the one worth the e2e: `enable`/`disable` answer `reloaded: false`,
 * and a page that redrew the pill on a toggle would tell an operator a plugin
 * had stopped while it was still serving tool calls.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const PLUGINS_URL = "/?v=settings&s=plugins";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

const HOSTINFO = {
  name: "genus-hostinfo",
  version: "0.1.0",
  enabled: true,
  recorded: false,
  verdict: "",
  state: "loaded",
  drifted: false,
  groups: ["genus.schemas", "genus.tools"],
  contributions: { tools: 1, schemas: 1 },
  failure_reason: null,
  manifest: { contract_version: 1, declared: { handlers: ["hostinfo"] } },
};

const LEDGER = {
  name: "genus-ledger",
  version: "1.0.0",
  enabled: true,
  recorded: false,
  verdict: "",
  state: "loaded",
  drifted: false,
  groups: ["genus.services"],
  contributions: { services: 2 },
  failure_reason: null,
  manifest: null,
};

/** Before `genus plugin sync` has ever run: no file, no rows, no verdicts. */
const FRESH = {
  generation: 1,
  lockfile: { path_configured: true, present: false, malformed: false, rows: 0 },
  plugins: [HOSTINFO, LEDGER],
};

/** After recording: a row each, and `verdict` is the placeholder it always is. */
const RECORDED = {
  generation: 1,
  lockfile: { path_configured: true, present: true, malformed: false, rows: 2 },
  plugins: [
    { ...HOSTINFO, recorded: true, verdict: "unscanned" },
    { ...LEDGER, recorded: true, verdict: "unscanned" },
  ],
};

/** After the reload: the engine has acted on the row the operator wrote. */
const AFTER_RELOAD = {
  generation: 2,
  lockfile: { path_configured: true, present: true, malformed: false, rows: 2 },
  plugins: [
    { ...HOSTINFO, recorded: true, verdict: "unscanned" },
    {
      ...LEDGER,
      recorded: true,
      verdict: "unscanned",
      enabled: false,
      state: "disabled",
      contributions: {},
      failure_reason: "disabled by operator",
    },
  ],
};

interface Recorded {
  posts: string[];
}

async function setupMocks(page: Page): Promise<Recorded> {
  const recorded: Recorded = { posts: [] };

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

  // The three acts. Which one has happened is what the listing below answers
  // with, so the whole flow is driven by what the operator has actually done.
  await page.route("**/api/bridge/api/plugins/**", (route) => {
    const url = route.request().url();
    recorded.posts.push(url);
    if (url.endsWith("/sync")) {
      return json(route, {
        recorded: ["genus-hostinfo", "genus-ledger"],
        added: ["genus-hostinfo", "genus-ledger"],
        updated: [],
        removed: [],
        reloaded: false,
      });
    }
    if (url.endsWith("/reload")) {
      return json(route, {
        generation: 2,
        loaded: 1,
        failures: [
          // The ENTRY-POINT name, not the distribution's — which is why the
          // page matches this back through the listing's groups.
          { name: "ledger", group: "genus.services", reason: "disabled by operator" },
        ],
      });
    }
    return json(route, {
      name: "genus-ledger",
      version: "1.0.0",
      manifest_sha256: "0".repeat(64),
      verdict: "unscanned",
      enabled: url.endsWith("/enable"),
      kinds: ["genus.services"],
      recorded_at: "2099-01-01T00:00:00+00:00",
      reloaded: false,
    });
  });

  await page.route("**/api/bridge/api/plugins", (route) => {
    const synced = recorded.posts.some((u) => u.endsWith("/sync"));
    const reloaded = recorded.posts.some((u) => u.endsWith("/reload"));
    return json(route, reloaded ? AFTER_RELOAD : synced ? RECORDED : FRESH);
  });

  return recorded;
}

test.describe("Settings › Plugins", () => {
  test("records a fresh install, disables one, and reloads the engine", async ({ page }) => {
    const recorded = await setupMocks(page);
    await page.goto(PLUGINS_URL, { waitUntil: "networkidle" });

    const screen = page.locator('[data-testid="settings-page-plugins"]');
    await expect(screen).toBeVisible({ timeout: 15000 });

    // 1. Nothing recorded: Record is the affordance, and no switch works.
    await expect(page.locator('[data-testid="plugins-record-empty"]')).toBeVisible();
    await expect(page.locator('[data-testid="plugin-switch-genus-ledger"]')).toBeDisabled();
    await expect(page.locator('[data-testid="plugin-hint-genus-ledger"]')).toBeVisible();
    await expect(page.locator('[data-testid="plugins-reload-bar"]')).toHaveCount(0);

    // 2. Record. The deltas are named, and the listing is re-read.
    await page.locator('[data-testid="plugins-record"]').click();
    await expect(page.locator('[data-testid="plugins-record-result"]')).toContainText(
      "genus-ledger"
    );
    await expect(page.locator('[data-testid="plugins-record-empty"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="plugin-switch-genus-ledger"]')).toBeEnabled();

    // Recording is not scanning: the word the lock row carries stays off screen.
    await expect(screen).not.toContainText("unscanned");

    // 3. Disable one. The switch moves; the state pill does not, because the
    //    engine is still running what it discovered.
    await page.locator('[data-testid="plugin-switch-genus-ledger"]').click();
    await expect(page.locator('[data-testid="plugin-switch-genus-ledger"]')).toHaveAttribute(
      "aria-checked",
      "false"
    );
    await expect(page.locator('[data-testid="plugin-state-genus-ledger"]')).toHaveAttribute(
      "data-state",
      "loaded"
    );
    await expect(page.locator('[data-testid="plugin-note-genus-ledger"]')).toContainText("reload");
    expect(recorded.posts).toContain(
      new URL("/api/bridge/api/plugins/genus-ledger/disable", page.url()).toString()
    );

    // 4. The bar is up because a row was written; reload applies it.
    await expect(page.locator('[data-testid="plugins-reload-bar"]')).toBeVisible();
    await page.locator('[data-testid="plugins-reload"]').click();

    await expect(page.locator('[data-testid="plugins-reload-result"]')).toContainText("1 plugin");
    // The operator's own decision, matched back to the distribution through
    // the listing's groups and rendered apart from a fault.
    await expect(
      page.locator('[data-testid="plugins-reload-intended-genus-ledger"]')
    ).toBeVisible();
    await expect(page.locator('[data-testid="plugins-reload-failed"]')).toHaveCount(0);

    // The listing was re-read, so the pill now tells the truth.
    await expect(page.locator('[data-testid="plugin-state-genus-ledger"]')).toHaveAttribute(
      "data-state",
      "disabled"
    );
    await expect(page.locator('[data-testid="plugins-reload-bar"]')).toHaveCount(0);
  });

  test("keeps every card and control usable on a 390 px screen", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(PLUGINS_URL, { waitUntil: "networkidle" });

    const screen = page.locator('[data-testid="settings-page-plugins"]');
    await expect(screen).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="plugins-record"]')).toBeVisible();
    await expect(page.locator('[data-testid="plugin-genus-hostinfo"]')).toBeVisible();
    await expect(page.locator('[data-testid="plugin-switch-genus-hostinfo"]')).toBeVisible();

    const worst = await page.evaluate(() => {
      let overflow = 0;
      let el: Element | null = document.querySelector('[data-testid="settings-page-plugins"]');
      while (el) {
        overflow = Math.max(overflow, el.scrollWidth - el.clientWidth);
        el = el.parentElement;
      }
      return overflow;
    });
    expect(worst).toBeLessThanOrEqual(1);
  });
});
