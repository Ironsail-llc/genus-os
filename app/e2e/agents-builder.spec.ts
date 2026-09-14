/**
 * The agent builder, end to end, against a bridge that lives entirely in
 * `page.route` — this spec must never reach a real one, because every act it
 * performs writes or deletes a manifest on the box.
 *
 * The mock keeps state, so the assertions are about the round trip and not
 * about a canned reply: the agent this spec creates has to appear in the next
 * listing, carry the cron it was later given, and be gone after it is retired.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const AGENTS_URL = "/?v=agents";

interface Manifest {
  id: string;
  name: string;
  description: string;
  version: string;
  department: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  delivery: string;
  model: string;
}

const SEED: Manifest = {
  id: "invoice-chaser",
  name: "Invoice Chaser",
  description: "Chases unpaid invoices every weekday morning.",
  version: "2026-09-01",
  department: "finance",
  cron: "0 9 * * 1-5",
  timezone: "UTC",
  enabled: true,
  delivery: "announce",
  model: "openrouter/openai/gpt-5.4",
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
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

interface Recorded {
  patches: Array<Record<string, unknown>>;
  deletes: Array<Record<string, unknown>>;
  creates: Array<Record<string, unknown>>;
}

async function setupMocks(page: Page): Promise<Recorded> {
  const store = new Map<string, Manifest>([[SEED.id, { ...SEED }]]);
  const recorded: Recorded = { patches: [], deletes: [], creates: [] };

  // An owner session: everything this spec does is operator-gated.
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

  await page.route("**/api/bridge/api/models", (route) => json(route, MODELS));

  // The two-segment routes — enable, disable, run — before the one-segment
  // ones, because a later `page.route` takes precedence in Playwright and
  // `/agent-manifests/*` would otherwise swallow `/agent-manifests/x/run`.
  await page.route("**/api/bridge/api/agent-manifests", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      recorded.creates.push(body);
      const id = String(body.id ?? "")
        || String(body.name ?? "")
          .trim()
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, "-")
          .replace(/^-+|-+$/g, "");
      store.set(id, {
        id,
        name: String(body.name ?? ""),
        description: String(body.description ?? ""),
        version: "2026-09-14",
        department: "",
        cron: String(body.cron ?? ""),
        timezone: String(body.timezone ?? ""),
        enabled: true,
        delivery: String(body.delivery_mode ?? "none"),
        model: String(body.model ?? ""),
      });
      return json(
        route,
        { id, manifest: { id }, warnings: [], reconcile: { applied: true } },
        201
      );
    }
    return json(route, {
      agents: [...store.values()].sort((a, b) => a.id.localeCompare(b.id)),
      broken: [{ id: "night-sweep", filename: "night-sweep.yaml", error_type: "ScannerError" }],
      count: store.size,
    });
  });

  await page.route("**/api/bridge/api/agent-manifests/*/*", (route) => {
    const path = new URL(route.request().url()).pathname;
    const id = path.split("/").slice(-2)[0];
    const agent = store.get(id);
    if (!agent) return json(route, { detail: "no such agent" }, 404);
    if (path.endsWith("/run")) return json(route, { id, triggered: { run_id: "r-e2e" } });
    agent.enabled = path.endsWith("/enable");
    return json(route, { id, manifest: {}, warnings: [], reconcile: { applied: true } });
  });

  await page.route("**/api/bridge/api/agent-manifests/*", (route) => {
    const method = route.request().method();
    const path = new URL(route.request().url()).pathname;
    const id = path.split("/").pop() as string;

    if (id === "validate") {
      return json(route, { ok: true, errors: [], warnings: [] });
    }

    const agent = store.get(id);
    if (!agent) return json(route, { detail: "no such agent" }, 404);

    if (method === "PATCH") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      recorded.patches.push(body);
      if (typeof body.cron === "string") agent.cron = body.cron;
      if (typeof body.timezone === "string") agent.timezone = body.timezone;
      if (typeof body.name === "string") agent.name = body.name;
      agent.version = "2026-09-14b";
      return json(route, {
        id,
        manifest: { ...agent, version: agent.version },
        warnings: [],
        pre_existing: [],
        reconcile: { applied: true },
      });
    }

    if (method === "DELETE") {
      recorded.deletes.push(route.request().postDataJSON() as Record<string, unknown>);
      store.delete(id);
      return json(route, { id, retired: true, reconcile: { applied: true } });
    }

    return json(route, {
      manifest: {
        id: agent.id,
        name: agent.name,
        description: agent.description,
        version: agent.version,
        instruction_file: `agents/${agent.id}.md`,
        model: { primary: agent.model, fallbacks: [] },
        schedule: { cron: agent.cron, timezone: agent.timezone, enabled: agent.enabled },
        delivery: { mode: agent.delivery, channel: "", to: "" },
        tools_allowed: [],
      },
      yaml: `id: ${agent.id}\nname: ${agent.name}\n`,
      instructions: "Do the job named above.",
      validation: { ok: true, errors: [], warnings: [] },
    });
  });

  return recorded;
}

test.describe("Agents › the builder", () => {
  test("creates an agent from three fields, schedules it, then retires it", async ({ page }) => {
    const recorded = await setupMocks(page);
    await page.goto(AGENTS_URL, { waitUntil: "networkidle" });

    // The manifest list, and the bucket of files the engine could not read.
    await expect(page.locator('[data-testid="agent-row-invoice-chaser"]')).toBeVisible({
      timeout: 15000,
    });
    await expect(page.locator('[data-testid="agent-manifests-broken"]')).toContainText(
      "night-sweep.yaml"
    );

    // Three fields, and the id the bridge is about to derive.
    await page.locator('[data-testid="agent-new"]').click();
    await page.locator('[data-testid="agent-field-name"]').fill("Vendor Follow Up");
    await expect(page.locator('[data-testid="agent-derived-id"]')).toContainText(
      "vendor-follow-up"
    );
    await page.locator('[data-testid="agent-field-job"]').fill("Chase vendors who have gone quiet.");
    await page
      .locator('[data-testid="agent-field-instructions"]')
      .fill("Write to any vendor silent for a week.");
    await page.locator('[data-testid="agent-create"]').click();

    await expect(page.locator('[data-testid="agent-panel-result"]')).toContainText(
      "vendor-follow-up"
    );
    expect(recorded.creates).toEqual([
      {
        name: "Vendor Follow Up",
        description: "Chase vendors who have gone quiet.",
        instructions: "Write to any vendor silent for a week.",
      },
    ]);

    // It is in the list, on the bridge's own answer and not on local state.
    await page.locator('[data-testid="agent-cancel"]').click();
    await expect(page.locator('[data-testid="agent-row-vendor-follow-up"]')).toBeVisible();

    // Advanced: give it a schedule, and read back what that cron means.
    await page.locator('[data-testid="agent-open-vendor-follow-up"]').click();
    await expect(page.locator('[data-testid="agent-field-name"]')).toHaveValue("Vendor Follow Up");
    await page.locator('[data-testid="agent-advanced-toggle"]').click();
    await page.locator('[data-testid="agent-field-cron"]').fill("0 9 * * 1-5");
    await expect(page.locator('[data-testid="agent-cron-preview"]')).toContainText("Monday");
    await page.locator('[data-testid="agent-field-timezone"]').selectOption("UTC");
    await page.locator('[data-testid="agent-save"]').click();

    await expect(page.locator('[data-testid="agent-panel-result"]')).toContainText("2026-09-14b");
    // Only what changed, with the change note the bridge writes to the changelog.
    expect(recorded.patches).toEqual([
      { cron: "0 9 * * 1-5", timezone: "UTC", change: "Edited via the Helm agent builder" },
    ]);
    await page.locator('[data-testid="agent-cancel"]').click();
    await expect(page.locator('[data-testid="agent-row-vendor-follow-up"]')).toContainText(
      "0 9 * * 1-5"
    );

    // Retire: the confirmation is the agent's own id, typed into a real input.
    await page.locator('[data-testid="agent-retire-vendor-follow-up"]').click();
    const confirm = page.locator('[data-testid="agent-retire-confirm-vendor-follow-up"]');
    await expect(confirm).toBeDisabled();
    await page
      .locator('[data-testid="agent-retire-input-vendor-follow-up"]')
      .fill("vendor-follow-up");
    await expect(confirm).toBeEnabled();
    await confirm.click();

    await expect(page.locator('[data-testid="agent-row-vendor-follow-up"]')).toHaveCount(0);
    expect(recorded.deletes).toEqual([{ confirm: "vendor-follow-up" }]);
    await expect(page.locator('[data-testid="agent-manifests-notice"]')).toContainText("retired");
  });

  test("stays usable at phone width", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(AGENTS_URL, { waitUntil: "networkidle" });

    const row = page.locator('[data-testid="agent-row-invoice-chaser"]');
    await expect(row).toBeVisible({ timeout: 15000 });

    // The table has actually collapsed into stacked cards here.
    const firstCell = row.locator("> *").first();
    expect(await firstCell.evaluate((el) => getComputedStyle(el).display)).toBe("flex");
    expect(await row.evaluate((el) => getComputedStyle(el).display)).toBe("block");

    // The panel is reachable and the page does not scroll sideways with it open.
    await page.locator('[data-testid="agent-new"]').click();
    await expect(page.locator('[data-testid="agent-panel"]')).toBeVisible();
    await page.locator('[data-testid="agent-advanced-toggle"]').click();
    await expect(page.locator('[data-testid="agent-advanced"]')).toBeVisible();

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(1);

    await page.setViewportSize({ width: 1440, height: 900 });
    expect(await firstCell.evaluate((el) => getComputedStyle(el).display)).toBe("table-cell");
  });
});
