/**
 * Settings › Providers, end to end, with every bridge route intercepted in the
 * browser — this spec must never reach a real bridge, because the act under
 * test is handing over a credential.
 *
 * The assertion that matters most is the last one: after a key has been typed
 * and saved, the typed value appears nowhere in the rendered page and nowhere
 * in any URL the browser asked for.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const SETTINGS_URL = "/?v=settings&s=providers";

/** The key this spec types. It must not survive anywhere but the request body. */
const CANDIDATE_KEY = "sk-e2e-never-render-me";

const PROVIDERS = {
  providers: [
    {
      id: "openrouter",
      label: "OpenRouter",
      configured: true,
      env_var: "OPENROUTER_API_KEY",
      default_model: "openrouter/openai/gpt-5.4",
      slots: [
        {
          position: 1,
          source: "vault",
          fingerprint: "sha256:11aa22bb",
          state: "active",
          updated_at: "2026-09-01T10:00:00+00:00",
        },
      ],
    },
    {
      id: "anthropic",
      label: "Anthropic",
      configured: true,
      env_var: "ANTHROPIC_API_KEY",
      default_model: "anthropic/claude-sonnet-4.6",
      slots: [
        {
          position: 1,
          source: "env",
          fingerprint: "sha256:55ee66ff",
          state: "active",
          updated_at: null,
        },
      ],
    },
    {
      id: "openai",
      label: "OpenAI",
      configured: false,
      env_var: "OPENAI_API_KEY",
      default_model: "openai/gpt-5.4",
      slots: [],
    },
  ],
};

const MODELS = {
  models: [
    {
      id: "openrouter/openai/gpt-5.4",
      provider: "openrouter",
      context_window: 400000,
      supports_thinking: true,
      supports_tools: true,
      source: "registry",
    },
    {
      id: "anthropic/claude-sonnet-4.6",
      provider: "anthropic",
      context_window: 200000,
      supports_thinking: true,
      supports_tools: true,
      source: "registry",
    },
  ],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

interface Recorded {
  putBodies: Array<Record<string, unknown>>;
  testBodies: Array<Record<string, unknown>>;
  urls: string[];
}

async function setupMocks(page: Page): Promise<Recorded> {
  const recorded: Recorded = { putBodies: [], testBodies: [], urls: [] };
  page.on("request", (request) => recorded.urls.push(request.url()));

  // An owner session: Settings is operator-gated in the shell.
  await page.route("**/api/auth/session", (route) =>
    json(route, {
      user: { name: "Operator", email: "operator@example.com" },
      role: "owner",
      expires: "2099-01-01T00:00:00.000Z",
    })
  );

  // The shell's own background traffic, kept off the box's real services.
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

  // The provider surface itself.
  await page.route("**/api/bridge/api/providers", (route) => json(route, PROVIDERS));
  await page.route("**/api/bridge/api/models", (route) => json(route, MODELS));
  await page.route("**/api/bridge/api/providers/*/test", (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    recorded.testBodies.push(body ?? {});
    const candidate = typeof body?.api_key === "string";
    return json(route, {
      ok: true,
      model: candidate ? "openai/gpt-5.4" : "openrouter/openai/gpt-5.4",
      latency_ms: candidate ? 311 : 412,
      error_class: null,
      message: "The provider answered.",
    });
  });
  await page.route("**/api/bridge/api/providers/*/keys/*", (route) => {
    if (route.request().method() !== "PUT") {
      return json(route, { configured: false, position: 1, removed: true });
    }
    recorded.putBodies.push(route.request().postDataJSON() as Record<string, unknown>);
    return json(route, { configured: true, fingerprint: "sha256:99887766", position: 1 });
  });

  return recorded;
}

test.describe("Settings › Providers", () => {
  test("lists providers, tests one, and swallows the key it is given", async ({ page }) => {
    const recorded = await setupMocks(page);
    await page.goto(SETTINGS_URL, { waitUntil: "networkidle" });

    // The table, with the digest and never a key.
    await expect(page.locator('[data-testid="providers-page"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="provider-row-openrouter"]')).toContainText(
      "sha256:11aa22bb"
    );
    await expect(page.locator('[data-testid="provider-row-anthropic"]')).toContainText("read-only");
    await expect(page.locator('[data-testid="provider-remove-anthropic-1"]')).toBeDisabled();

    // Test connection on a stored credential.
    await page.locator('[data-testid="provider-test-openrouter"]').click();
    await expect(page.locator('[data-testid="provider-test-result-openrouter"]')).toContainText(
      "412ms"
    );

    // Add a key: test it first, then save it.
    await page.locator('[data-testid="provider-add-openai"]').click();
    const input = page.locator('[data-testid="provider-key-input-openai"]');
    await expect(input).toHaveAttribute("type", "password");
    await input.fill(CANDIDATE_KEY);

    await page.locator('[data-testid="provider-key-test-openai"]').click();
    await expect(page.locator('[data-testid="provider-key-test-result-openai"]')).toContainText(
      "311ms"
    );

    await page.locator('[data-testid="provider-key-save-openai"]').click();
    await expect(input).toHaveCount(0);

    // The key reached the bridge in a body — and nowhere else.
    expect(recorded.putBodies).toEqual([{ api_key: CANDIDATE_KEY }]);
    expect(recorded.testBodies).toContainEqual({ api_key: CANDIDATE_KEY });
    expect(await page.content()).not.toContain(CANDIDATE_KEY);
    for (const url of recorded.urls) expect(url).not.toContain(CANDIDATE_KEY);
    expect(page.url()).not.toContain(CANDIDATE_KEY);
  });

  test("stays usable at phone width", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(SETTINGS_URL, { waitUntil: "networkidle" });

    const row = page.locator('[data-testid="provider-row-openrouter"]');
    await expect(row).toBeVisible({ timeout: 15000 });

    // The table has actually collapsed: the row and its cells are stacked
    // boxes here, and real table parts at desktop width.
    const firstCell = row.locator("> *").first();
    expect(await firstCell.evaluate((el) => getComputedStyle(el).display)).toBe("flex");
    expect(await row.evaluate((el) => getComputedStyle(el).display)).toBe("block");

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(1);

    await page.setViewportSize({ width: 1440, height: 900 });
    expect(await firstCell.evaluate((el) => getComputedStyle(el).display)).toBe("table-cell");
  });
});
