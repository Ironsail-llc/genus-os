/**
 * Settings › Users & roles, end to end, with every bridge route intercepted in
 * the browser — this spec must never reach a real bridge, because the acts
 * under test create sign-in accounts and change what they are allowed to do.
 *
 * The two assertions that matter: the invite reaches the bridge as exactly the
 * body the route takes and the grant that comes back is shown once with its
 * issuer and expiry; and a refused role change is rendered in the SERVER's
 * words, because "only an owner may grant the owner role" is an instruction
 * and "something went wrong" is not.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const SETTINGS_URL = "/?v=settings&s=users";

const OWNER_ID = "11111111-1111-4111-8111-111111111111";
const MEMBER_ID = "22222222-2222-4222-8222-222222222222";

const ROLES = {
  roles: [
    { id: "admin", description: "Administers the instance: users, agents, credentials, settings." },
    { id: "member", description: "Everyday access — chat, the CRM, and the agents' output." },
    {
      id: "owner",
      description:
        "Runs this instance. Full access, and the only role the appliance requires exactly one of.",
    },
    { id: "viewer", description: "Read-only, plus chat. What a paired channel sender gets." },
  ],
};

const USERS = {
  users: [
    {
      id: OWNER_ID,
      email: "alice@example.com",
      display_name: "Alice",
      role: "owner",
      status: "active",
      sso_bound: true,
      mfa_enabled: true,
      last_login_at: "2099-01-01T08:00:00+00:00",
      created_at: "2098-01-02T09:00:00+00:00",
    },
    {
      id: MEMBER_ID,
      email: "bob@example.com",
      display_name: "Bob",
      role: "member",
      status: "active",
      sso_bound: false,
      mfa_enabled: false,
      last_login_at: null,
      created_at: "2098-05-02T09:00:00+00:00",
    },
  ],
  count: 2,
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

/**
 * The widest values a real instance can hand this page: a display name a
 * workspace admin set to 120 characters and a 64-character address local part.
 * The 390 px test is only worth anything against strings like these.
 */
function hostileUsers() {
  return {
    users: [
      USERS.users[0],
      { ...USERS.users[1], display_name: "N".repeat(120), email: `${"a".repeat(64)}@example.com` },
    ],
    count: 2,
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

interface Recorded {
  invites: Array<Record<string, unknown>>;
  patches: Array<{ url: string; body: Record<string, unknown> }>;
  grants: string[];
}

async function setupMocks(page: Page, hostile = false): Promise<Recorded> {
  const recorded: Recorded = { invites: [], patches: [], grants: [] };

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

  await page.route("**/api/bridge/api/auth/roles", (route) => json(route, ROLES));
  // Registered before `…/users/*` so it is matched first: that glob does not
  // cross a `/`, so without this route the arm-grant call fell through to the
  // 503 catch-all and the affordance was never exercised end to end.
  await page.route("**/api/bridge/api/users/*/binding-grant", (route) => {
    recorded.grants.push(route.request().url());
    return json(
      route,
      {
        grant: {
          id: "66666666-6666-4666-8666-666666666666",
          expires_at: "2099-01-01T01:00:00+00:00",
          issuer: "https://idp.example.com",
        },
      },
      201
    );
  });
  await page.route("**/api/bridge/api/users/*", (route) => {
    const url = route.request().url();
    const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
    recorded.patches.push({ url, body });
    // The appliance holds exactly one owner ROW, so a promotion to owner is a
    // refusal the operator has to read rather than a change to retry.
    if (body.role === "owner") {
      return json(route, { detail: "that tenant already has an owner account" }, 409);
    }
    return json(route, { user: { ...USERS.users[1], ...body } });
  });
  await page.route("**/api/bridge/api/users", (route) => {
    if (route.request().method() !== "POST") return json(route, hostile ? hostileUsers() : USERS);
    const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
    recorded.invites.push(body);
    return json(
      route,
      {
        user: {
          id: "33333333-3333-4333-8333-333333333333",
          email: body.email,
          display_name: body.display_name ?? "Carol",
          role: body.role,
          status: body.sso ? "active" : "invited",
          sso_bound: false,
          mfa_enabled: false,
          last_login_at: null,
          created_at: "2099-01-01T00:00:00+00:00",
        },
        ...(body.sso
          ? {
              grant: {
                id: "55555555-5555-4555-8555-555555555555",
                expires_at: "2099-01-01T01:00:00+00:00",
                issuer: "https://idp.example.com",
              },
            }
          : {}),
      },
      201
    );
  });

  return recorded;
}

test.describe("Settings › Users & roles", () => {
  test("invites with SSO, shows the grant once, and prints a refused role change", async ({
    page,
  }) => {
    const recorded = await setupMocks(page);
    await page.goto(SETTINGS_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="users-page"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator(`[data-testid="user-row-${OWNER_ID}"]`)).toContainText(
      "alice@example.com"
    );
    await expect(page.locator(`[data-testid="user-last-login-${MEMBER_ID}"]`)).toHaveText("never");

    // Invite, with SSO.
    await page.locator('[data-testid="users-invite-open"]').click();
    await page.locator('[data-testid="invite-email"]').fill("carol@example.com");
    await page.locator('[data-testid="invite-display-name"]').fill("Carol");
    await page.locator('[data-testid="invite-role"]').selectOption("member");
    await page.locator('[data-testid="invite-sso"]').check();
    await page.locator('[data-testid="invite-submit"]').click();

    const grant = page.locator('[data-testid="invite-grant"]');
    await expect(grant).toBeVisible();
    await expect(grant).toContainText("https://idp.example.com");
    await expect(grant).toContainText("carol@example.com");
    expect(recorded.invites).toEqual([
      { email: "carol@example.com", role: "member", display_name: "Carol", sso: true },
    ]);

    // Shown once: dismissing it is the only exit, and it does not come back.
    await page.locator('[data-testid="invite-grant-dismiss"]').click();
    await expect(grant).toHaveCount(0);

    // A role change the appliance refuses, in the appliance's own words.
    await page.locator(`[data-testid="user-role-${MEMBER_ID}"]`).selectOption("owner");
    await expect(page.locator(`[data-testid="user-error-${MEMBER_ID}"]`)).toHaveText(
      "that tenant already has an owner account"
    );
    // Refused, not retried, and the select shows what the account still is.
    expect(recorded.patches.filter((call) => call.body.role === "owner")).toHaveLength(1);
    await expect(page.locator(`[data-testid="user-role-${MEMBER_ID}"]`)).toHaveValue("member");

    // A change the appliance allows lands, and says what it did.
    await page.locator(`[data-testid="user-role-${MEMBER_ID}"]`).selectOption("viewer");
    await expect(page.locator(`[data-testid="user-note-${MEMBER_ID}"]`)).toContainText("viewer");

    // Arm a fresh grant on an existing account — the other half of the SSO
    // story, and the one the glob on `…/users/*` used to swallow.
    await page.locator(`[data-testid="user-grant-arm-${MEMBER_ID}"]`).click();
    const rowGrant = page.locator(`[data-testid="user-grant-${MEMBER_ID}"]`);
    await expect(rowGrant).toBeVisible();
    await expect(rowGrant).toContainText("https://idp.example.com");
    await expect(rowGrant).toContainText("bob@example.com");
    expect(recorded.grants).toHaveLength(1);
    expect(recorded.grants[0]).toContain(`/api/users/${MEMBER_ID}/binding-grant`);
  });

  test("stays usable at phone width, with the widest strings the API can send", async ({
    page,
  }) => {
    await setupMocks(page, true);
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(SETTINGS_URL, { waitUntil: "networkidle" });

    await expect(page.locator(`[data-testid="user-row-${MEMBER_ID}"]`)).toBeVisible({
      timeout: 15000,
    });
    expect(await worstOverflow(page, "settings-page-users")).toBeLessThanOrEqual(1);

    await page.locator('[data-testid="users-invite-open"]').click();
    await expect(page.locator('[data-testid="users-invite-form"]')).toBeVisible();
    expect(await worstOverflow(page, "settings-page-users")).toBeLessThanOrEqual(1);
  });
});
