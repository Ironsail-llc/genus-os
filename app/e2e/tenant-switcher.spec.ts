/**
 * Tenant switching, end to end, with every backend intercepted in the browser.
 *
 * The session, the accessible-tenant listing, the switch route and the task
 * data are all mocked and share one piece of state — which tenant the session
 * acts in — so the spec proves what the operator sees: pick a child tenant in
 * the top bar, the page reloads onto that tenant's data under an "Acting as"
 * banner, and "Switch back" returns home. A child-tenant admin, whose listing
 * holds only its own tenant, gets no switcher at all.
 *
 * The cookie handling itself is unit-tested (`__tests__/lib/auth-tenant-switch.test.ts`);
 * nothing here may reach a real bridge or engine.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const TASKS_URL = "/?v=tasks";

interface Tenant {
  id: string;
  display_name: string;
  parent_tenant_id: string | null;
  is_home: boolean;
}

const PARENT: Tenant = { id: "acme-parent", display_name: "Acme Parent", parent_tenant_id: null, is_home: true };
const CHILD: Tenant = {
  id: "acme-child-a",
  display_name: "Acme Child A",
  parent_tenant_id: "acme-parent",
  is_home: false,
};

const TASKS: Record<string, { id: string; title: string; status: string }[]> = {
  "acme-parent": [{ id: "p1", title: "Parent quarterly review", status: "TODO" }],
  "acme-child-a": [{ id: "c1", title: "Child onboarding checklist", status: "TODO" }],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

interface Identity {
  home: string;
  accessible: Tenant[];
}

async function setupMocks(page: Page, identity: Identity) {
  const state = { acting: identity.home, switches: [] as string[] };

  await page.route("**/api/auth/session", (route) =>
    json(route, {
      user: { name: "Alice", email: "alice@example.com" },
      role: "admin",
      tenantId: state.acting,
      homeTenantId: identity.home,
      backendAuthorized: true,
      expires: "2099-01-01T00:00:00.000Z",
    }),
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
    }),
  );
  await page.route("**/api/actions/execute", (route) => {
    const body = route.request().postDataJSON() as { tool?: string } | null;
    const data = body?.tool === "agent_status" ? { agents: [] } : { tasks: TASKS[state.acting] ?? [] };
    return json(route, { data });
  });

  // Registered FIRST, so it is matched LAST: nothing addressed at the bridge
  // falls through to a real one.
  await page.route("**/api/bridge/**", (route) =>
    json(route, { detail: "this route is not intercepted by the spec" }, 503),
  );
  await page.route("**/api/bridge/api/tenants/accessible", (route) => json(route, identity.accessible));
  await page.route("**/api/session/switch-tenant", (route) => {
    const { tenant } = route.request().postDataJSON() as { tenant: string };
    if (!identity.accessible.some((t) => t.id === tenant)) {
      return json(route, { error: "tenant not accessible" }, 403);
    }
    state.switches.push(tenant);
    state.acting = tenant;
    return json(route, { tenant, homeTenant: identity.home });
  });

  return state;
}

test.describe("tenant switcher", () => {
  test("a parent admin switches to a child tenant, sees its data, and switches back", async ({ page }) => {
    const state = await setupMocks(page, { home: "acme-parent", accessible: [PARENT, CHILD] });

    await page.goto(TASKS_URL);
    await expect(page.getByText("Parent quarterly review")).toBeVisible();
    await expect(page.getByTestId("acting-tenant-banner")).toHaveCount(0);

    const switcher = page.getByLabel("Tenant");
    await expect(switcher).toBeVisible();
    await switcher.selectOption("acme-child-a");

    await expect(page.getByTestId("acting-tenant-banner")).toContainText("Acting as Acme Child A");
    await expect(page.getByText("Child onboarding checklist")).toBeVisible();
    await expect(page.getByText("Parent quarterly review")).toHaveCount(0);
    await expect(page.getByLabel("Tenant")).toHaveValue("acme-child-a");

    await page.getByRole("button", { name: "Switch back" }).click();

    await expect(page.getByTestId("acting-tenant-banner")).toHaveCount(0);
    await expect(page.getByText("Parent quarterly review")).toBeVisible();
    expect(state.switches).toEqual(["acme-child-a", "acme-parent"]);
  });

  test("a child-tenant admin sees no switcher", async ({ page }) => {
    await setupMocks(page, { home: "acme-child-a", accessible: [{ ...CHILD, is_home: true }] });

    await page.goto(TASKS_URL);
    await expect(page.getByText("Child onboarding checklist")).toBeVisible();

    await expect(page.getByTestId("tenant-switcher")).toHaveCount(0);
    await expect(page.getByLabel("Tenant")).toHaveCount(0);
    await expect(page.getByTestId("acting-tenant-banner")).toHaveCount(0);
  });
});
