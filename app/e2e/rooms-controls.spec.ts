/**
 * The workspace controls in the real browser: settings (save, a stale-form
 * conflict, a refusal shown on its field), a shared user's read-only drawer
 * with "Duplicate as mine", the share dialog's run-as warning, a preview run
 * only its requester sees, the period selector (stored at once, missing ones
 * built), the admin's "All company workspaces", and all of it at phone width.
 *
 * Every backend is intercepted with replies shaped like the shared-threads
 * contract; the rooms bridge and engine are not needed.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const ROOM = "11111111-1111-4111-8111-111111111111";
const COPY = "66666666-6666-4666-8666-666666666666";
const EDITION = "aaaaaaaa-0000-4000-8000-000000000002";
const OLDER = "aaaaaaaa-0000-4000-8000-000000000001";
const BUILT = "aaaaaaaa-0000-4000-8000-000000000003";
const PREVIEW = "aaaaaaaa-0000-4000-8000-000000000009";

const SUMMARY = {
  id: ROOM,
  kind: "report",
  title: "Weekly sales",
  agentId: "main",
  paused: false,
  archived: false,
  unread: 0,
  lastActivityAt: "2026-11-20T09:00:00Z",
};

function detail(access: string, rev = 3) {
  return {
    ...SUMMARY,
    instructions: "Summarise the week's sales.",
    settings: { tz: "UTC", default_grain: "week", schedule_text: "every Monday at 08:00", notify_default: true },
    rev,
    access,
    runsAs: { userId: "u-alice", displayName: "Alice" },
    members: [
      { userId: "u-alice", displayName: "Alice", assigned: true },
      { userId: "u-bob", displayName: "Bob", assigned: false },
    ],
    nextRunAt: "2026-11-23T08:00:00Z",
  };
}

function edition(id: string, seq: number, title: string, start: string, end: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    seq,
    kind: "edition",
    status: "published",
    preview: false,
    periodStart: start,
    periodEnd: end,
    grain: "week",
    revision: 3,
    title,
    summary: "Revenue up on the week.",
    blocks: [{ component: "render_markdown", props: { content: `Notes for ${title}.` } }],
    suggestions: [],
    provenance: {
      run_id: `run-${seq}`,
      ran_as: "u-alice",
      started_at: "2026-11-16T08:00:00Z",
      tool_calls: [{ tool: "crm_query", at: "2026-11-16T08:00:04Z", ok: true }],
    },
    createdAt: "2026-11-16T08:05:00Z",
    ...extra,
  };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

interface Options {
  access: string;
  role?: string;
}

async function setupMocks(page: Page, { access, role = "member" }: Options) {
  const sent: Array<{ method: string; path: string; body: unknown }> = [];
  let rev = 3;
  let built = false;
  let builtReads = 0;
  const editions = [
    edition(EDITION, 2, "Week 46", "2026-11-09", "2026-11-15"),
    edition(OLDER, 1, "Week 45", "2026-11-02", "2026-11-08"),
  ];

  await page.route("**/api/auth/session", (route) =>
    json(route, { user: { name: "Alice", email: "alice@example.com" }, role, expires: "2099-01-01T00:00:00Z" }),
  );
  await page.route("**/api/bridge/api/agent-manifests", (route) => json(route, { agents: [], default_agent: "main" }));
  await page.route("**/api/chat/plan/status*", (route) => json(route, { active: false }));
  await page.route("**/api/chat/deep/status*", (route) => json(route, { active: false }));
  await page.route("**/api/session", (route) => json(route, {}));
  await page.route("**/api/dashboard/welcome", (route) => json(route, { html: "<div>Welcome</div>", type: "html" }));
  await page.route("**/api/dashboard/generate", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/health", (route) => json(route, { status: "healthy", agents: {} }));
  await page.route("**/api/events/stream*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: "event: sync\ndata: {}\n\n",
    }),
  );
  await page.route("**/api/chat/history*", (route) => json(route, { messages: [], hasMore: false }));


  await page.route("**/api/bridge/api/rooms**", (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace("/api/bridge/api/rooms", "");
    const method = req.method();
    const body = req.postData() ? JSON.parse(req.postData() || "{}") : undefined;
    if (method !== "GET") sent.push({ method, path, body });

    if (path === "" && url.searchParams.get("scope") === "all") {
      return json(route, { rooms: [{ ...detail("admin", rev), members: detail("admin").members }] });
    }
    if (path === "") return json(route, { rooms: [SUMMARY] });
    if (path === "/directory") {
      return json(route, { users: [ { userId: "u-alice", displayName: "Alice" }, { userId: "u-bob", displayName: "Bob" }, { userId: "u-carol", displayName: "Carol" } ] });
    }
    if (path === `/${ROOM}` && method === "GET") return json(route, detail(access, rev));
    if (path === `/${ROOM}` && method === "PATCH") {
      const changes = (body as { changes: Record<string, unknown> }).changes;
      if (changes.scheduleText === "whenever") {
        return json(route, { detail: { error: "invalid_schedule", message: "Could not read the schedule 'whenever'", field: "scheduleText" } }, 422);
      }
      if ((body as { expectedRev: number }).expectedRev !== rev) {
        return json(route, { detail: { error: "revision_conflict", currentRev: rev } }, 409);
      }
      rev += 1;
      return json(route, { ...detail(access, rev), ...(changes.title ? { title: changes.title } : {}) });
    }
    if (path === `/${ROOM}/read`) return json(route, { ok: true });
    if (path === `/${ROOM}/editions`) return json(route, { editions: built ? [edition(BUILT, 3, "Tue 17 Nov", "2026-11-17", "2026-11-17", { grain: "day" }), ...editions] : editions });
    if (path === `/${ROOM}/editions/${BUILT}`) {
      // Still building on the first look; published on the next poll.
      builtReads += 1;
      built = builtReads > 1;
      return json(route, edition(BUILT, 3, "Tue 17 Nov", "2026-11-17", "2026-11-17", { grain: "day", status: built ? "published" : "building" }));
    }
    if (path === `/${ROOM}/editions/${PREVIEW}`) {
      return json(route, edition(PREVIEW, 9, "Preview of week 46", "2026-11-09", "2026-11-15", { preview: true }));
    }
    if (path === `/${ROOM}/periods`) {
      return json(route, edition(BUILT, 3, "Tue 17 Nov", "2026-11-17", "2026-11-17", { grain: "day", status: "building" }));
    }
    if (path === `/${ROOM}/run`) {
      const preview = (body as { preview?: boolean })?.preview === true;
      return json(
        route,
        edition(preview ? PREVIEW : BUILT, 9, "Preview of week 46", "2026-11-09", "2026-11-15", { preview, status: "building" }),
      );
    }
    if (path === `/${ROOM}/revisions`) {
      return json(route, {
        revisions: [
          { rev: 1, changedBy: "u-alice", changedByName: "Alice", changedAt: "2026-11-01T09:00:00Z", definition: { title: "Weekly sales", instructions: "Summarise sales.", settings: { schedule_text: "every Monday at 08:00" } } },
          { rev: 2, changedBy: "u-alice", changedByName: "Alice", changedAt: "2026-11-08T09:00:00Z", definition: { title: "Weekly sales", instructions: "Summarise the week's sales.", settings: { schedule_text: "every Monday at 08:00" } } },
        ],
      });
    }
    if (path === `/${ROOM}/duplicate`) return json(route, { ...detail("assigned"), id: COPY, title: "Weekly sales (copy)" });
    if (path === `/${COPY}`) return json(route, { ...detail("assigned"), id: COPY, title: "Weekly sales (copy)" });
    if (path === `/${COPY}/editions`) return json(route, { editions: [] });
    if (path === `/${COPY}/read`) return json(route, { ok: true });
    if (path.startsWith(`/${ROOM}/members/`)) return json(route, detail(access, rev));
    if (path === `/${ROOM}/reassign`) return json(route, detail("admin", rev));
    if (path === `/${ROOM}/notify`) return json(route, { inApp: true, muted: false, channels: {}, linkedChannels: [] });
    return json(route, { detail: "Not found" }, 404);
  });
  return { sent, bumpRev: () => (rev += 1) };
}

async function openRoom(page: Page) {
  await page.goto(`/?v=chat&room=${ROOM}`, { waitUntil: "networkidle" });
  await expect(page.locator('[data-testid="room-view"]')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('[data-testid="room-pinned-edition"]')).toBeVisible();
}

test.describe("Workspaces — controls", () => {
  test("an assigned user saves settings, meets a stale-form conflict and a refused schedule", async ({ page }) => {
    const { sent, bumpRev } = await setupMocks(page, { access: "assigned" });
    await openRoom(page);

    await page.getByTestId("room-settings-open").click();
    const drawer = page.getByTestId("room-settings");
    await expect(drawer).toBeVisible();
    await expect(drawer.getByTestId("settings-next-run")).toContainText("23 Nov");

    await drawer.getByLabel(/^title/i).fill("Weekly revenue");
    await drawer.getByTestId("settings-save").click();
    await expect(drawer.getByTestId("settings-saved")).toBeVisible();
    expect(sent.find((r) => r.method === "PATCH")?.body).toEqual({ expectedRev: 3, changes: { title: "Weekly revenue" } });

    // Someone else saves meanwhile: the next save from this form is refused, not merged.
    bumpRev();
    await drawer.getByLabel(/^instructions/i).fill("Summarise the week's sales by region.");
    await drawer.getByTestId("settings-save").click();
    await expect(drawer.getByTestId("settings-conflict")).toContainText(/changed since you opened/i);
    await drawer.getByTestId("settings-conflict").getByRole("button", { name: /reload/i }).click();
    await expect(drawer.getByTestId("settings-conflict")).toHaveCount(0);

    await drawer.getByLabel(/schedule/i).fill("whenever");
    await drawer.getByTestId("settings-save").click();
    await expect(drawer.getByTestId("field-error-scheduleText")).toHaveText("Could not read the schedule 'whenever'");

    await drawer.getByTestId("settings-tab-history").click();
    await expect(drawer.getByTestId("revision-2")).toContainText(/instructions/i);
    await expect(drawer.getByTestId("revision-restore-1")).toBeVisible();
  });

  test("the share dialog says who it runs as and what sharing exposes, and shares", async ({ page }) => {
    const { sent } = await setupMocks(page, { access: "assigned" });
    await openRoom(page);
    await page.getByTestId("room-share-open").click();
    const dialog = page.getByTestId("share-dialog");
    await expect(dialog.getByTestId("share-runs-as")).toHaveText(/Runs as Alice/);
    await expect(dialog.getByTestId("share-warning")).toContainText("data fetched with Alice's access");
    await dialog.getByTestId("share-person").selectOption("u-carol");
    await dialog.getByTestId("share-add").click();
    await expect.poll(() => sent.filter((r) => r.method === "PUT").map((r) => r.path)).toContain(`/${ROOM}/members/u-carol`);
  });

  test("a preview run is labelled as only for its requester and opens beside, not in, the dashboard", async ({ page }) => {
    const { sent } = await setupMocks(page, { access: "assigned" });
    await openRoom(page);
    const button = page.getByTestId("room-preview-run");
    await expect(button).toHaveAccessibleDescription(/only you/i);
    await button.click();
    const panel = page.getByTestId("room-preview");
    await expect(panel).toContainText(/only you can see/i);
    await expect(panel.getByTestId("edition-title")).toHaveText("Preview of week 46", { timeout: 15000 });
    await expect(panel.getByTestId("room-preview-badge")).toHaveText("Preview");
    expect(sent.find((r) => r.path === `/${ROOM}/run`)?.body).toEqual({ preview: true });
    await expect(page.getByTestId("room-pinned-edition").getByTestId("edition-title")).toHaveText("Week 46");
  });

  test("a shared user reads settings, may duplicate as mine, and cannot share", async ({ page }) => {
    await setupMocks(page, { access: "shared" });
    await openRoom(page);
    await expect(page.getByTestId("room-share-open")).toHaveCount(0);
    await page.getByTestId("room-settings-open").click();
    const drawer = page.getByTestId("room-settings");
    await expect(drawer.getByTestId("settings-readonly")).toBeVisible();
    await expect(drawer.getByTestId("settings-save")).toHaveCount(0);
    await drawer.getByTestId("settings-duplicate").click();
    await expect(page).toHaveURL(new RegExp(`room=${COPY}`));
    await expect(page.locator('[data-testid="room-view"] h1')).toHaveText("Weekly sales (copy)");
  });

  test("picking periods: a stored one at once, a missing one built, one not over yet refused", async ({ page }) => {
    const { sent } = await setupMocks(page, { access: "shared" });
    await openRoom(page);
    const pinnedTitle = page.getByTestId("room-pinned-edition").getByTestId("edition-title");
    await expect(pinnedTitle).toHaveText("Week 46");

    await page.getByTestId("period-trigger").click();
    await page.getByTestId("period-grain-week").click();
    await page.getByTestId("period-anchor").fill("2026-11-04");
    await page.getByTestId("period-apply").click();
    await expect(pinnedTitle).toHaveText("Week 45");
    expect(sent.filter((r) => r.path === `/${ROOM}/periods`)).toHaveLength(0);

    await page.getByTestId("period-trigger").click();
    await page.getByTestId("period-grain-day").click();
    // A period not over yet is refused as it is picked: said, and no button.
    await page.getByTestId("period-anchor").fill("2099-01-01");
    await expect(page.getByTestId("period-error")).toBeVisible();
    await expect(page.getByTestId("period-apply")).toBeDisabled();
    expect(sent.filter((r) => r.path === `/${ROOM}/periods`)).toHaveLength(0);

    await page.getByTestId("period-anchor").fill("2026-11-17");
    await page.getByTestId("period-apply").click();
    await expect(page.getByTestId("period-status")).toContainText(/building/i);
    await expect(pinnedTitle).toHaveText("Tue 17 Nov", { timeout: 15000 });
    expect(sent.filter((r) => r.path === `/${ROOM}/periods`)[0]?.body).toEqual({ grain: "day", anchor: "2026-11-17" });
  });

  test("an admin opens all company workspaces from the rail and reassigns one", async ({ page }) => {
    const { sent } = await setupMocks(page, { access: "admin", role: "admin" });
    await openRoom(page);
    await page.getByTestId("rooms-admin-entry").click();
    const row = page.getByTestId(`rooms-admin-row-${ROOM}`);
    await expect(row).toContainText("Weekly sales");
    await expect(row).toContainText("Alice");
    await row.getByTestId(`rooms-admin-reassign-${ROOM}`).click();
    await row.getByTestId(`rooms-admin-reassign-to-${ROOM}`).selectOption("u-bob");
    await row.getByTestId(`rooms-admin-reassign-confirm-${ROOM}`).click();
    await expect.poll(() => sent.find((r) => r.path === `/${ROOM}/reassign`)?.body).toEqual({ userId: "u-bob" });
  });
});

test.describe("Workspaces — controls at phone width (C8)", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("the rail collapses, the dashboard stacks above the conversation, and every control is usable", async ({ page }) => {
    await setupMocks(page, { access: "assigned" });
    await openRoom(page);

    await expect(page.getByTestId("room-rail-toggle").first()).toBeVisible();
    const pane = await page.getByTestId("room-editions").boundingBox();
    const input = page.locator('[data-testid="room-view"] [data-testid="chat-input"]');
    await expect(input).toBeVisible();
    const composer = await input.boundingBox();
    expect(pane && composer && pane.y < composer.y).toBeTruthy();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

    // Header controls fit, and the drawers open at full width.
    await expect(page.getByTestId("room-settings-open")).toBeVisible();
    await page.getByTestId("room-settings-open").click();
    const drawer = page.getByTestId("room-settings");
    await expect(drawer).toBeVisible();
    const box = await drawer.boundingBox();
    expect(box && box.width >= 380).toBeTruthy();
    await expect(drawer.getByTestId("settings-save")).toBeVisible();
    await page.keyboard.press("Escape");

    await page.getByTestId("period-trigger").click();
    await expect(page.getByTestId("period-apply")).toBeVisible();
    await page.getByTestId("period-grain-week").click();
    await page.getByTestId("period-anchor").fill("2026-11-04");
    await page.getByTestId("period-apply").click();
    await expect(page.getByTestId("room-pinned-edition").getByTestId("edition-title")).toHaveText("Week 45");

    await page.getByTestId("room-more").click();
    await expect(page.getByTestId("room-menu-duplicate")).toBeVisible();
  });
});
