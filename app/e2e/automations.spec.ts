/**
 * Automations, end to end, against a bridge that lives entirely in `page.route`.
 *
 * This spec must never reach a real bridge: two of the acts it performs —
 * resetting a circuit breaker and PATCHing a manifest's cron — would put a
 * production agent back on a schedule, or take it off one.
 *
 * The mock keeps state, so the assertions are about the round trip rather than
 * a canned reply: a breaker the operator resets has to be untripped in the NEXT
 * listing, and a saved cron has to come back in it.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const AUTOMATIONS_URL = "/?v=workflows";

const TRIPPED_ID = "ledger-sweeper";
const HEALTHY_ID = "invoice-chaser";

const HEARTBEAT_ID = "orchestrator:heartbeat";

interface Automation {
  id: string;
  agent_id: string;
  kind: string;
  editable: boolean;
  manifest_unreadable: boolean;
  name: string;
  description: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  next_run_at: string | null;
  last_run: Record<string, unknown> | null;
  consecutive_errors: number;
  breaker_tripped: boolean;
  breaker_threshold: number;
  delivery: { mode: string; channel: string; to: string };
}

function seed(): Automation[] {
  return [
    {
      id: HEALTHY_ID,
      agent_id: HEALTHY_ID,
      kind: "agent",
      editable: true,
      manifest_unreadable: false,
      name: "Invoice Chaser",
      description: "Chases unpaid invoices.",
      cron: "0 9 * * *",
      timezone: "UTC",
      enabled: true,
      next_run_at: "2099-01-01T09:00:00Z",
      last_run: {
        id: "11111111-1111-4111-8111-111111111111",
        started_at: "2026-06-15T09:00:00Z",
        status: "completed",
        duration_ms: 4200,
        delivery_status: "delivered",
        delivered_at: "2026-06-15T09:01:00Z",
        delivery_channel: "telegram",
        delivery_mode: "announce",
        verified_status: "verified",
        outcome_assessment: "successful",
      },
      consecutive_errors: 0,
      breaker_tripped: false,
      breaker_threshold: 5,
      delivery: { mode: "announce", channel: "telegram", to: "agent@example.com" },
    },
    {
      id: TRIPPED_ID,
      agent_id: TRIPPED_ID,
      kind: "agent",
      editable: true,
      manifest_unreadable: false,
      name: "Ledger Sweeper",
      description: "",
      cron: "*/15 * * * *",
      timezone: "UTC",
      enabled: true,
      next_run_at: null,
      last_run: {
        id: "22222222-2222-4222-8222-222222222222",
        started_at: "2026-06-15T11:45:00Z",
        status: "failed",
        duration_ms: 900,
        delivery_status: null,
        delivered_at: null,
        delivery_channel: null,
        delivery_mode: "none",
        verified_status: null,
        outcome_assessment: null,
      },
      consecutive_errors: 5,
      breaker_tripped: true,
      breaker_threshold: 5,
      delivery: { mode: "none", channel: "", to: "" },
    },
    // A job the engine keys as `<agent>:heartbeat` — the shape that had no card
    // at all before the C1 fix, and the shape the primary agent actually has.
    {
      id: HEARTBEAT_ID,
      agent_id: "orchestrator",
      kind: "heartbeat",
      editable: false,
      manifest_unreadable: false,
      name: "Orchestrator · heartbeat",
      description: "",
      cron: "0 12,16 * * *",
      timezone: "UTC",
      enabled: true,
      next_run_at: "2099-01-01T12:00:00Z",
      last_run: null,
      consecutive_errors: 0,
      breaker_tripped: false,
      breaker_threshold: 5,
      delivery: { mode: "announce", channel: "telegram", to: "agent@example.com" },
    },
  ];
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

interface Recorded {
  patches: Array<{ id: string; body: Record<string, unknown> }>;
  resets: string[];
}

async function setupMocks(page: Page): Promise<Recorded> {
  const store = new Map(seed().map((row) => [row.id, row]));
  const recorded: Recorded = { patches: [], resets: [] };

  // An owner session: the listing and every act below it are operator-gated.
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
  await page.route("**/api/bridge/api/approvals", (route) => json(route, { count: 0, pending: [] }));
  await page.route("**/api/bridge/api/workflows", (route) => json(route, []));

  await page.route("**/api/bridge/api/automations", (route) => {
    const automations = [...store.values()];
    return json(route, { automations, count: automations.length });
  });

  await page.route("**/api/bridge/api/automations/*/reset-breaker", (route) => {
    const id = decodeURIComponent(
      new URL(route.request().url()).pathname.split("/").slice(-2)[0]
    );
    recorded.resets.push(id);
    const row = store.get(id);
    if (!row) return json(route, { detail: "no schedule for that automation" }, 404);
    store.set(id, { ...row, consecutive_errors: 0, breaker_tripped: false });
    return json(route, { id, consecutive_errors: 0 });
  });

  await page.route("**/api/bridge/api/agent-manifests/*", (route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop() as string;
    if (route.request().method() !== "PATCH") return json(route, { ok: true });
    const body = route.request().postDataJSON() as Record<string, unknown>;
    recorded.patches.push({ id, body });
    const row = store.get(id);
    if (row) {
      store.set(id, {
        ...row,
        cron: String(body.cron ?? row.cron),
        timezone: String(body.timezone ?? row.timezone),
      });
    }
    return json(route, { saved: true, in_effect: true });
  });

  return recorded;
}

test.describe("Automations › run truth and the breaker", () => {
  test("resets a tripped breaker and edits the schedule in place", async ({ page }) => {
    const recorded = await setupMocks(page);
    await page.goto(AUTOMATIONS_URL, { waitUntil: "networkidle" });

    await expect(page.locator(`[data-testid="automation-card-${HEALTHY_ID}"]`)).toBeVisible({
      timeout: 15000,
    });

    // Three columns, three different facts about the same run.
    await expect(page.locator(`[data-testid="automation-ran-${HEALTHY_ID}"]`)).toContainText(
      "completed"
    );
    await expect(page.locator(`[data-testid="automation-delivered-${HEALTHY_ID}"]`)).toContainText(
      "telegram"
    );
    await expect(page.locator(`[data-testid="automation-completed-${HEALTHY_ID}"]`)).toContainText(
      "verified"
    );

    // A deliberately silent agent is not a delivery failure.
    await expect(page.locator(`[data-testid="automation-delivered-${TRIPPED_ID}"]`)).toContainText(
      "none expected"
    );

    // A heartbeat job has a card of its own, labelled, with no schedule form:
    // the manifest PATCH owns schedule.* only.
    await expect(page.locator(`[data-testid="automation-card-${HEARTBEAT_ID}"]`)).toBeVisible();
    await expect(page.locator(`[data-testid="automation-kind-${HEARTBEAT_ID}"]`)).toHaveText(
      "heartbeat"
    );
    await expect(page.locator(`[data-testid="automation-edit-${HEARTBEAT_ID}"]`)).toHaveCount(0);
    await expect(page.locator(`[data-testid="automation-uneditable-${HEARTBEAT_ID}"]`)).toContainText(
      "heartbeat"
    );

    // The tripped one, and only the tripped one, carries the chip. And while it
    // is tripped it must not advertise a next fire it will not have.
    await expect(page.locator(`[data-testid="automation-next-${TRIPPED_ID}"]`)).toContainText(
      "breaker"
    );
    await expect(page.locator(`[data-testid="automation-breaker-${TRIPPED_ID}"]`)).toContainText(
      "5"
    );
    await expect(page.locator(`[data-testid="automation-breaker-${HEALTHY_ID}"]`)).toHaveCount(0);

    await page.locator(`[data-testid="automation-reset-${TRIPPED_ID}"]`).click();
    // Gone from the NEXT listing, not merely hidden by local state.
    await expect(page.locator(`[data-testid="automation-breaker-${TRIPPED_ID}"]`)).toHaveCount(0, {
      timeout: 15000,
    });
    expect(recorded.resets).toEqual([TRIPPED_ID]);

    // Editing the cron: the preview reads the new expression back before the
    // save, and the card carries the new next-run text afterwards.
    const before = await page
      .locator(`[data-testid="automation-next-${HEALTHY_ID}"]`)
      .textContent();

    await page.locator(`[data-testid="automation-edit-${HEALTHY_ID}"]`).click();
    await page.locator(`[data-testid="automation-cron-input-${HEALTHY_ID}"]`).fill("30 7 * * 1-5");
    await page
      .locator(`[data-testid="automation-change-input-${HEALTHY_ID}"]`)
      .fill("Moved to weekday mornings.");
    await expect(
      page.locator(`[data-testid="automation-edit-preview-${HEALTHY_ID}"]`)
    ).toContainText("7:30");
    await page.locator(`[data-testid="automation-save-${HEALTHY_ID}"]`).click();

    await expect(page.locator(`[data-testid="automation-cron-${HEALTHY_ID}"]`)).toContainText(
      "7:30",
      { timeout: 15000 }
    );
    const after = await page
      .locator(`[data-testid="automation-next-${HEALTHY_ID}"]`)
      .textContent();
    expect(after).not.toBe(before);

    expect(recorded.patches).toEqual([
      {
        id: HEALTHY_ID,
        body: {
          cron: "30 7 * * 1-5",
          timezone: "UTC",
          change: "Moved to weekday mornings.",
        },
      },
    ]);
  });

  test("a save the engine did not reconcile does not claim it did", async ({ page }) => {
    await setupMocks(page);
    // Exactly what the bridge answers with the engine unreachable: a 200 that
    // says the file was written and the schedule was NOT picked up.
    await page.route("**/api/bridge/api/agent-manifests/*", (route) =>
      route.request().method() === "PATCH"
        ? json(route, { saved: true, reconcile: { applied: false, error: "engine unreachable" } })
        : json(route, { ok: true })
    );
    await page.goto(AUTOMATIONS_URL, { waitUntil: "networkidle" });

    await expect(page.locator(`[data-testid="automation-card-${HEALTHY_ID}"]`)).toBeVisible({
      timeout: 15000,
    });
    await page.locator(`[data-testid="automation-edit-${HEALTHY_ID}"]`).click();
    await page.locator(`[data-testid="automation-cron-input-${HEALTHY_ID}"]`).fill("0 6 * * *");
    await page.locator(`[data-testid="automation-save-${HEALTHY_ID}"]`).click();

    await expect(page.locator(`[data-testid="automation-reconcile-${HEALTHY_ID}"]`)).toContainText(
      "engine unreachable",
      { timeout: 15000 }
    );
    await expect(page.locator(`[data-testid="automation-note-${HEALTHY_ID}"]`)).not.toContainText(
      "re-derived"
    );
  });

  test("stays usable at phone width", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(AUTOMATIONS_URL, { waitUntil: "networkidle" });

    const card = page.locator(`[data-testid="automation-card-${TRIPPED_ID}"]`);
    await expect(card).toBeVisible({ timeout: 15000 });

    // The claim that actually breaks at 390 px is sideways scroll on the
    // document, not one card's box — a long cron phrase or an unbreakable
    // agent id widens the page, not the card that contains it.
    const metrics = await page.evaluate(() => ({
      docScroll: document.documentElement.scrollWidth,
      inner: window.innerWidth,
    }));
    expect(metrics.docScroll).toBeLessThanOrEqual(metrics.inner);

    const box = await card.boundingBox();
    expect(box).not.toBeNull();
    expect((box?.width ?? 0) + (box?.x ?? 0)).toBeLessThanOrEqual(391);

    await expect(page.locator(`[data-testid="automation-reset-${TRIPPED_ID}"]`)).toBeVisible();
    await expect(page.locator(`[data-testid="automation-run-${TRIPPED_ID}"]`)).toBeVisible();
  });
});
