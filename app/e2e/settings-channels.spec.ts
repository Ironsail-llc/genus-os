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

/**
 * Built fresh per `setupMocks`, never shared at module scope: the approve
 * handler settles the code by emptying this list, and a fixture two tests in
 * one worker both mutate makes the second test measure a page the first one
 * changed.
 */
function freshPending() {
  return {
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
}

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

/**
 * The widest values a real instance can hand this page: a 120-character
 * display name, a 60-character access mode and a long channel name. The 390 px
 * test is only worth anything against strings like these — short fixtures pass
 * a responsive assertion no matter how the page is built.
 */
function hostileChannels() {
  return {
    channels: [
      {
        ...CHANNELS.channels[0],
        access_mode: `pairing-${"x".repeat(52)}`,
        health: {
          channel: "telegram",
          configured: true,
          bot_token: `sha256:${"a".repeat(64)}`,
        },
      },
      CHANNELS.channels[1],
    ],
  };
}

function hostileIdentities() {
  return {
    channel: "telegram",
    identities: [
      {
        ...IDENTITIES.identities[0],
        display_name: "N".repeat(120),
        user_id: "u".repeat(64),
        native_id_fingerprint: `sha256:${"b".repeat(64)}`,
      },
    ],
    count: 1,
  };
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

async function setupMocks(page: Page, hostile = false): Promise<Recorded> {
  const recorded: Recorded = { verify: [], approve: [] };
  const pending = freshPending();

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
    pending.pending.length = 0;
    pending.count = 0;
    return json(route, { id: IDENTITY_ID, channel: "telegram", role: "member" });
  });
  await page.route("**/api/bridge/api/channels/*/pending", (route) => json(route, pending));
  await page.route("**/api/bridge/api/channels/*/identities", (route) =>
    json(route, hostile ? hostileIdentities() : IDENTITIES)
  );
  await page.route("**/api/bridge/api/channels", (route) =>
    json(route, hostile ? hostileChannels() : CHANNELS)
  );

  return recorded;
}

test.describe("Settings › Channels", () => {
  test("lists channels, verifies one, and approves a pairing", async ({ page }) => {
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

  test("stays usable at phone width, with the widest strings the API can send", async ({
    page,
  }) => {
    await setupMocks(page, true);
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(SETTINGS_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="channel-card-telegram"]')).toBeVisible({
      timeout: 15000,
    });
    expect(await worstOverflow(page, "settings-page-channels")).toBeLessThanOrEqual(1);

    // The access panel and the pairing form are the widest surfaces here, and
    // neither is on screen until the operator opens them.
    await page.locator('[data-testid="channel-access-toggle-telegram"]').click();
    await expect(page.locator('[data-testid="channel-access-telegram"]')).toBeVisible();
    expect(await worstOverflow(page, "settings-page-channels")).toBeLessThanOrEqual(1);

    await page.locator(`[data-testid="pairing-settle-${PENDING_ID}"]`).click();
    await expect(page.locator(`[data-testid="pairing-form-${PENDING_ID}"]`)).toBeVisible();
    expect(await worstOverflow(page, "settings-page-channels")).toBeLessThanOrEqual(1);
  });
});
