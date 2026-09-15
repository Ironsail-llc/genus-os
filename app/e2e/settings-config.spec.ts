/**
 * Settings › Config, end to end, with every bridge route intercepted in the
 * browser — this spec must never reach a real bridge, because the act under
 * test rewrites an instance's configuration file.
 *
 * Three assertions carry the spec:
 *
 * * a save posts ONLY the fields that changed, typed the way the field is
 *   declared — the route is all-or-nothing, so a batch carrying untouched
 *   fields lets one unrelated bad value block the edit;
 * * a 200 banners the units the PATCH itself named, and the banner survives
 *   the operator going to another Settings page and coming back, because the
 *   API keeps no memory of it;
 * * a 422 lands on the field in the SERVER's words and marks nothing saved —
 *   "'nine' is not a valid value" is an instruction, "something went wrong" is
 *   not.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const CONFIG_URL = "/?v=settings&s=config";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function field(overrides: Record<string, unknown>) {
  return {
    aliases: [],
    type: "str",
    description: "",
    default: null,
    secret: false,
    governed: false,
    restart_required: true,
    restart_units: ["robothor-engine"],
    since: "legacy",
    hot: false,
    ...overrides,
  };
}

const SCHEMA = {
  groups: [
    {
      id: "engine",
      label: "Engine",
      fields: [
        field({
          name: "ROBOTHOR_MAX_CONCURRENT_AGENTS",
          env: "ROBOTHOR_MAX_CONCURRENT_AGENTS",
          field: "engine.max_concurrent_agents",
          group: "engine",
          type: "int",
          description: "How many agent runs may execute at once.",
          default: 3,
        }),
        field({
          name: "ROBOTHOR_LOG_DIR",
          env: "ROBOTHOR_LOG_DIR",
          field: "engine.log_dir",
          group: "engine",
          description: "Where the engine writes its logs.",
          default: "/var/log/robothor",
        }),
        field({
          name: "ROBOTHOR_ENGINE_HOST",
          env: "ROBOTHOR_ENGINE_HOST",
          field: "engine.host",
          group: "engine",
          description: "Address the engine binds.",
          default: "127.0.0.1",
        }),
      ],
    },
    {
      id: "channels",
      label: "Channels",
      fields: [
        field({
          name: "ROBOTHOR_TELEGRAM_BOT_TOKEN",
          env: "ROBOTHOR_TELEGRAM_BOT_TOKEN",
          field: "channels.telegram_bot_token",
          group: "channels",
          description: "The Telegram bot's API token.",
          secret: true,
        }),
      ],
    },
  ],
};

function values() {
  return {
    values: {
      ROBOTHOR_MAX_CONCURRENT_AGENTS: { value: 3, source: "default", editable: true },
      ROBOTHOR_LOG_DIR: { value: "/var/log/robothor", source: "config", editable: true },
      ROBOTHOR_ENGINE_HOST: { value: "0.0.0.0", source: "env", editable: false },
      ROBOTHOR_TELEGRAM_BOT_TOKEN: {
        value: { configured: true, fingerprint: "sha256:ab12cd34" },
        source: "env",
        editable: false,
      },
    },
    pending_restart: [],
  };
}

interface Recorded {
  patches: Array<Record<string, unknown>>;
}

/**
 * How far the widest ancestor of `testid` overflows its own box.
 *
 * NOT `document.documentElement`: the settings body is an `overflow-y-auto`
 * container, which makes its `overflow-x` compute to `auto` — so it absorbs
 * everything its children overflow by and the document never widens. An
 * assertion on the document therefore cannot fail, whatever the page does.
 */
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

async function setupMocks(page: Page): Promise<Recorded> {
  const recorded: Recorded = { patches: [] };

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
  // can fall through to the real one. A 503 rather than an empty body, because
  // a route this spec forgot must be loud on the page, not silently empty.
  await page.route("**/api/bridge/**", (route) =>
    json(route, { detail: "this route is not intercepted by the spec" }, 503)
  );

  await page.route("**/api/bridge/api/settings/schema", (route) => json(route, SCHEMA));
  await page.route("**/api/bridge/api/settings", (route) => {
    if (route.request().method() !== "PATCH") return json(route, values());
    const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
    recorded.patches.push(body);
    const changes = (body.changes ?? {}) as Record<string, unknown>;

    // The bridge validates the WHOLE batch first and writes nothing if any of
    // it fails, so a bad concurrency value refuses the log directory with it.
    if (changes.ROBOTHOR_MAX_CONCURRENT_AGENTS === "nine") {
      return json(
        route,
        {
          applied: [],
          pending_restart: [],
          errors: [
            {
              name: "ROBOTHOR_MAX_CONCURRENT_AGENTS",
              message:
                "ROBOTHOR_MAX_CONCURRENT_AGENTS: 'nine' is not a valid value (Input should be a valid integer)",
            },
          ],
        },
        422
      );
    }
    return json(route, {
      applied: Object.keys(changes),
      pending_restart: ["robothor-engine"],
      errors: [],
    });
  });

  return recorded;
}

test.describe("Settings › Config", () => {
  test("saves two fields, banners the restart, and lands a refusal on its field", async ({
    page,
  }) => {
    const recorded = await setupMocks(page);
    await page.goto(CONFIG_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="settings-page-config"]')).toBeVisible({
      timeout: 15000,
    });

    // A secret is a status, never a box — and its value is nowhere on the page.
    await page.locator('[data-testid="config-group-toggle-channels"]').click();
    const secretRow = page.locator('[data-testid="config-field-ROBOTHOR_TELEGRAM_BOT_TOKEN"]');
    await expect(secretRow).toContainText("sha256:ab12cd34");
    await expect(secretRow.locator("input")).toHaveCount(0);

    await page.locator('[data-testid="config-group-toggle-engine"]').click();

    // A field the environment supplies is read-only, and says why.
    await expect(page.locator('[data-testid="config-readonly-ROBOTHOR_ENGINE_HOST"]')).toContainText(
      "wins over config.yaml"
    );
    await expect(
      page.locator('[data-testid="config-field-ROBOTHOR_ENGINE_HOST"] input')
    ).toHaveCount(0);

    await page.locator('[data-testid="config-input-ROBOTHOR_LOG_DIR"]').fill("/var/log/genus");
    await page.locator('[data-testid="config-input-ROBOTHOR_MAX_CONCURRENT_AGENTS"]').fill("7");
    await page.locator('[data-testid="config-save-engine"]').click();

    await expect(page.locator('[data-testid="config-saved-ROBOTHOR_LOG_DIR"]')).toBeVisible();
    await expect(page.locator('[data-testid="config-restart-banner"]')).toContainText(
      "robothor-engine"
    );

    expect(recorded.patches).toHaveLength(1);
    expect(recorded.patches[0].changes).toEqual({
      ROBOTHOR_LOG_DIR: "/var/log/genus",
      ROBOTHOR_MAX_CONCURRENT_AGENTS: 7,
    });

    // The banner is client state and Settings unmounts an inactive page, so
    // this is the trip that would lose it.
    await page.locator('[data-testid="settings-nav-flags"]').click();
    await page.locator('[data-testid="settings-nav-config"]').click();
    await expect(page.locator('[data-testid="config-restart-banner"]')).toContainText(
      "robothor-engine"
    );
    await page.locator('[data-testid="config-restart-dismiss"]').click();
    await expect(page.locator('[data-testid="config-restart-banner"]')).toHaveCount(0);

    // Now the refusal. Nothing is written, and the sentence is the server's.
    await page.locator('[data-testid="config-group-toggle-engine"]').click();
    await page
      .locator('[data-testid="config-input-ROBOTHOR_MAX_CONCURRENT_AGENTS"]')
      .evaluate((el) => {
        // A number input will not hold "nine" through fill(); the bridge's own
        // refusal is what this asserts, so the value is set the way a paste
        // into a text field would arrive.
        const input = el as HTMLInputElement;
        input.type = "text";
      });
    await page.locator('[data-testid="config-input-ROBOTHOR_MAX_CONCURRENT_AGENTS"]').fill("nine");
    await page.locator('[data-testid="config-save-engine"]').click();

    await expect(
      page.locator('[data-testid="config-error-ROBOTHOR_MAX_CONCURRENT_AGENTS"]')
    ).toContainText("is not a valid value");
    await expect(
      page.locator('[data-testid="config-saved-ROBOTHOR_MAX_CONCURRENT_AGENTS"]')
    ).toHaveCount(0);
  });

  test("filters to one setting, and fits a 390 px screen", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(CONFIG_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="settings-page-config"]')).toBeVisible({
      timeout: 15000,
    });

    await page.locator('[data-testid="config-search"]').fill("concurrent");
    await expect(
      page.locator('[data-testid="config-field-ROBOTHOR_MAX_CONCURRENT_AGENTS"]')
    ).toBeVisible();
    await expect(page.locator('[data-testid="config-group-channels"]')).toHaveCount(0);

    await page.locator('[data-testid="config-search"]').fill("");
    await page.locator('[data-testid="config-group-toggle-engine"]').click();
    await expect(page.locator('[data-testid="config-field-ROBOTHOR_LOG_DIR"]')).toBeVisible();

    expect(await worstOverflow(page, "settings-page-config")).toBeLessThanOrEqual(1);
  });
});
