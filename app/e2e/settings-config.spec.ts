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

/**
 * The route's own refusal for ROBOTHOR_ENGINE_HOST, verbatim.
 *
 * It names a DEPRECATED ALIAS on purpose: `provenance.env_name_in_use` walks
 * `(env, *aliases)`, so on a mid-migration box the variable actually supplying
 * a field is not the canonical name — and a page that composed this sentence
 * itself would send the operator to clear one that is not set.
 */
const ENV_REFUSAL =
  "ROBOTHOR_ENGINE_HOST is set in this instance's environment (ROBOTHOR_OLD_ENGINE_HOST), " +
  "which wins over config.yaml — a change saved here would apply to nothing. Clear the " +
  "variable on the box and restart robothor-engine, then it can be managed from this page.";

const SECRET_REFUSAL =
  "ROBOTHOR_TELEGRAM_BOT_TOKEN holds a credential. `genus config set` never writes secrets -- " +
  "config.yaml is a plain file that gets copied into bug reports. Store it with " +
  "`genus vault set <key>` and give the service the key.";

function values() {
  return {
    values: {
      ROBOTHOR_MAX_CONCURRENT_AGENTS: {
        value: 3,
        source: "default",
        editable: true,
        reason: null,
      },
      ROBOTHOR_LOG_DIR: {
        value: "/var/log/robothor",
        source: "config",
        editable: true,
        reason: null,
      },
      ROBOTHOR_ENGINE_HOST: {
        value: "0.0.0.0",
        source: "env",
        editable: false,
        reason: ENV_REFUSAL,
      },
      ROBOTHOR_TELEGRAM_BOT_TOKEN: {
        value: { configured: true, fingerprint: "sha256:ab12cd34" },
        source: "env",
        editable: false,
        reason: SECRET_REFUSAL,
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

    // An empty log directory is a refusal an operator can actually PRODUCE
    // through this form — they clear the box and press Save. (Feeding "nine"
    // to the int field would need the spec to retype the input as text, which
    // tests a path the product cannot reach.)
    if (changes.ROBOTHOR_LOG_DIR === "") {
      return json(
        route,
        {
          applied: [],
          pending_restart: [],
          errors: [
            {
              name: "ROBOTHOR_LOG_DIR",
              message:
                "ROBOTHOR_LOG_DIR: '' is not a valid value (String should have at least 1 character)",
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

    // A field the environment supplies is read-only, in the SERVER's words —
    // including the variable it resolved, which this page cannot compute.
    await expect(page.locator('[data-testid="config-readonly-ROBOTHOR_ENGINE_HOST"]')).toHaveText(
      ENV_REFUSAL
    );
    await expect(page.locator('[data-testid="config-readonly-ROBOTHOR_ENGINE_HOST"]')).toContainText(
      "ROBOTHOR_OLD_ENGINE_HOST"
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

    // Now the refusal — produced the way an operator would produce it, by
    // clearing the box. Nothing is written, and the sentence is the server's.
    await page.locator('[data-testid="config-group-toggle-engine"]').click();
    await page.locator('[data-testid="config-input-ROBOTHOR_LOG_DIR"]').fill("");
    await page.locator('[data-testid="config-save-engine"]').click();

    await expect(page.locator('[data-testid="config-error-ROBOTHOR_LOG_DIR"]')).toContainText(
      "String should have at least 1 character"
    );
    await expect(page.locator('[data-testid="config-saved-ROBOTHOR_LOG_DIR"]')).toHaveCount(0);
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
