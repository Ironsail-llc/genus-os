/**
 * Settings › Channels, end to end, with every bridge route intercepted in the
 * browser.
 *
 * This spec must never reach a real bridge: `POST /api/channels/{name}/verify`
 * may really send a message on the channel it is verifying, and approving a
 * pairing binds a real sender to a real account. Both are intercepted here, and
 * the recorded requests are what the assertions are made against.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const SETTINGS_URL = "/?v=settings&s=channels";

const PENDING_ID = "11111111-1111-4111-8111-111111111111";
const IDENTITY_ID = "33333333-3333-4333-8333-333333333333";

const CHANNELS = {
  channels: [
    {
      name: "telegram",
      builtin: true,
      configured: true,
      health: { channel: "telegram", configured: true, bot_token: "sha256:aabbccddeeff" },
      verify_available: true,
      access_mode: "pairing",
      pending_pairings: 1,
    },
    {
      name: "pager_relay",
      builtin: false,
      configured: null,
      health: { channel: "pager_relay", timed_out: true, error: "health() did not answer in 5s" },
      verify_available: false,
      access_mode: null,
      pending_pairings: null,
    },
  ],
};

const PENDING = {
  channel: "telegram",
  pending: [
    {
      id: PENDING_ID,
      channel: "telegram",
      expires_at: "2099-01-01T00:30:00+00:00",
      created_at: "2099-01-01T00:00:00+00:00",
      display_name_present: true,
    },
  ],
  count: 1,
};

const IDENTITIES = {
  channel: "telegram",
  identities: [
    {
      id: IDENTITY_ID,
      user_id: "alice",
      native_id_fingerprint: "sha256:99887766",
      display_name: "Alice",
      role: "member",
      paired_at: "2099-01-01T00:00:00+00:00",
      paired_by: "operator:someone",
    },
  ],
  count: 1,
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

interface Recorded {
  verify: Array<{ url: string; body: Record<string, unknown> }>;
  approve: Array<{ url: string; body: Record<string, unknown> }>;
}

async function setupMocks(page: Page): Promise<Recorded> {
  const recorded: Recorded = { verify: [], approve: [] };

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

  // Registered FIRST, so it is matched LAST: nothing addressed at the bridge
  // can fall through to the real one. A 503 rather than an empty body, because
  // a route this spec forgot must be loud on the page, not silently empty.
  await page.route("**/api/bridge/**", (route) =>
    json(route, { detail: "this route is not intercepted by the spec" }, 503)
  );

  // The channel surface itself. Playwright matches route handlers in reverse
  // registration order, so the broadest pattern is registered first.
  await page.route("**/api/bridge/api/channels/*/verify", (route) => {
    recorded.verify.push({
      url: route.request().url(),
      body: (route.request().postDataJSON() ?? {}) as Record<string, unknown>,
    });
    return json(route, {
      channel: "telegram",
      configured: true,
      verify_available: true,
      error_class: null,
      steps: [
        { step: "auth", ok: true, detail: "bot reachable" },
        { step: "post", ok: false, detail: "chat not found" },
      ],
    });
  });
  await page.route("**/api/bridge/api/channels/*/pairings/*/approve", (route) => {
    recorded.approve.push({
      url: route.request().url(),
      body: (route.request().postDataJSON() ?? {}) as Record<string, unknown>,
    });
    // Settled: the next read of `pending` answers with nobody waiting.
    PENDING.pending.length = 0;
    PENDING.count = 0;
    return json(route, { id: IDENTITY_ID, channel: "telegram", role: "member" });
  });
  await page.route("**/api/bridge/api/channels/*/pending", (route) => json(route, PENDING));
  await page.route("**/api/bridge/api/channels/*/identities", (route) => json(route, IDENTITIES));
  await page.route("**/api/bridge/api/channels", (route) => json(route, CHANNELS));

  return recorded;
}

test.describe("Settings › Channels", () => {
  test("lists channels, verifies one, and approves a pairing", async ({ page }) => {
    // Restored per test: the approve mock empties it on the way through.
    PENDING.pending = [
      {
        id: PENDING_ID,
        channel: "telegram",
        expires_at: "2099-01-01T00:30:00+00:00",
        created_at: "2099-01-01T00:00:00+00:00",
        display_name_present: true,
      },
    ];
    PENDING.count = 1;

    const recorded = await setupMocks(page);
    await page.goto(SETTINGS_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="channels-page"]')).toBeVisible({ timeout: 15000 });

    // The three-valued fields, each painted as itself.
    await expect(page.locator('[data-testid="channel-configured-telegram"]')).toHaveText(
      "Configured"
    );
    await expect(page.locator('[data-testid="channel-configured-pager_relay"]')).toHaveText(
      "Unknown"
    );
    await expect(page.locator('[data-testid="channel-pending-count-pager_relay"]')).toContainText(
      "could not be read"
    );
    // A plugin channel that cannot prove anything is offered no Verify button.
    await expect(page.locator('[data-testid="channel-verify-pager_relay"]')).toHaveCount(0);

    // Credentials are added on the box, and this page offers no field for one.
    await expect(page.locator('[data-testid="channels-credential-hint"]')).toContainText(
      "genus channel add"
    );
    await expect(page.locator('input[type="password"]')).toHaveCount(0);

    // Verify, with a target the operator typed.
    await page.locator('[data-testid="channel-verify-target-telegram"]').fill("C0PLACEHOLDER");
    await page.locator('[data-testid="channel-verify-telegram"]').click();
    await expect(page.locator('[data-testid="channel-verify-result-telegram"]')).toHaveAttribute(
      "data-verdict",
      "failed"
    );
    await expect(
      page.locator('[data-testid="channel-verify-step-telegram-post"]')
    ).toContainText("chat not found");
    expect(recorded.verify).toHaveLength(1);
    expect(recorded.verify[0].body).toEqual({ target: "C0PLACEHOLDER" });

    // Approve the sender who is waiting, with the code they were given.
    await page.locator('[data-testid="channel-access-toggle-telegram"]').click();
    await expect(page.locator(`[data-testid="channel-pending-${PENDING_ID}"]`)).toBeVisible();
    await expect(page.locator(`[data-testid="channel-identity-${IDENTITY_ID}"]`)).toContainText(
      "Alice"
    );

    await page.locator(`[data-testid="pairing-settle-${PENDING_ID}"]`).click();
    await page.locator(`[data-testid="pairing-code-${PENDING_ID}"]`).fill("K7M2PH");
    await page.locator(`[data-testid="pairing-email-${PENDING_ID}"]`).fill("alice@example.com");
    await page.locator(`[data-testid="pairing-role-${PENDING_ID}"]`).selectOption("member");
    await page.locator(`[data-testid="pairing-approve-${PENDING_ID}"]`).click();

    await expect.poll(() => recorded.approve.length).toBe(1);
    expect(recorded.approve[0].url).toContain("/pairings/K7M2PH/approve");
    expect(recorded.approve[0].body).toEqual({ email: "alice@example.com", role: "member" });
    await expect(page.locator(`[data-testid="channel-pending-empty-telegram"]`)).toBeVisible();
  });

  test("stays usable at phone width", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(SETTINGS_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="channel-card-telegram"]')).toBeVisible({
      timeout: 15000,
    });
    await page.locator('[data-testid="channel-access-toggle-telegram"]').click();
    await expect(page.locator('[data-testid="channel-access-telegram"]')).toBeVisible();

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });
});
