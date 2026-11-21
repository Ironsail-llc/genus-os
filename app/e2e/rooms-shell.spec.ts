/**
 * A workspace in the real browser: open it from its link, see its latest
 * edition pinned as the dashboard, reload and find the charts still drawn
 * (from the edition, not from a render marker the history strips), then post
 * a message and see it go to the room by id.
 *
 * Every backend is intercepted with replies shaped exactly like the
 * shared-threads contract; the rooms bridge and engine are not needed.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const ROOM = "11111111-1111-4111-8111-111111111111";
const EDITION = "aaaaaaaa-0000-4000-8000-000000000002";
const OLDER = "aaaaaaaa-0000-4000-8000-000000000001";

const SUMMARY = {
  id: ROOM,
  kind: "report",
  title: "Weekly sales",
  agentId: "main",
  paused: false,
  archived: false,
  unread: 2,
  lastActivityAt: "2026-11-20T09:00:00Z",
};

const ROOM_DETAIL = {
  ...SUMMARY,
  instructions: "Summarise the week's sales.",
  settings: { tz: "UTC", default_grain: "week" },
  rev: 1,
  access: "shared",
  runsAs: { userId: "u-alice", displayName: "Alice" },
  members: [
    { userId: "u-alice", displayName: "Alice", assigned: true },
    { userId: "u-bob", displayName: "Bob", assigned: false },
  ],
  nextRunAt: "2026-11-23T08:00:00Z",
};

function edition(id: string, seq: number, title: string, start: string, end: string) {
  return {
    id,
    seq,
    kind: "edition",
    status: "published",
    preview: false,
    periodStart: start,
    periodEnd: end,
    grain: "week",
    revision: 1,
    title,
    summary: "Revenue up on the week.",
    blocks: [
      {
        component: "render_bar_chart",
        props: {
          title: `Revenue by day (${title})`,
          data: [
            { name: "Mon", value: 3 },
            { name: "Tue", value: 5 },
          ],
          dataKey: "value",
        },
      },
    ],
    suggestions: [],
    provenance: { run_id: "run-1", ran_as: "u-alice", started_at: "2026-11-22T08:00:00Z", tool_calls: [] },
    createdAt: "2026-11-22T08:05:00Z",
  };
}

const EDITIONS = [
  edition(EDITION, 2, "Week 47", "2026-11-16", "2026-11-22"),
  edition(OLDER, 1, "Week 46", "2026-11-09", "2026-11-15"),
];

const HISTORY = [
  { id: 10, role: "user", content: "How did we do?", authorUserId: "u-bob", authorName: "Bob" },
  { id: 11, role: "assistant", content: "Week 47 is out.", authorName: "Analyst", editionId: EDITION },
];

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function setupMocks(page: Page) {
  const sends: Array<Record<string, unknown>> = [];
  const reads: string[] = [];
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
  await page.route("**/api/chat/history*", (route) => {
    const url = new URL(route.request().url());
    return json(route, { messages: url.searchParams.get("room") === ROOM ? HISTORY : [], hasMore: false });
  });
  await page.route("**/api/chat/send", (route) => {
    sends.push(JSON.parse(route.request().postData() || "{}"));
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: `event: done\ndata: ${JSON.stringify({ text: "Margins held at 31%." })}\n\n`,
    });
  });
  await page.route("**/api/bridge/api/rooms**", (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/bridge/api/rooms", "");
    if (path === "") return json(route, { rooms: [SUMMARY] });
    if (path === `/${ROOM}`) return json(route, ROOM_DETAIL);
    if (path === `/${ROOM}/read` && route.request().method() === "POST") {
      reads.push(ROOM);
      return json(route, { ok: true });
    }
    if (path === `/${ROOM}/editions`) return json(route, { editions: EDITIONS });
    if (path === `/${ROOM}/editions/${EDITION}`) return json(route, EDITIONS[0]);
    if (path === `/${ROOM}/editions/${OLDER}`) return json(route, EDITIONS[1]);
    return json(route, { error: "Not found" }, 404);
  });
  return { sends, reads };
}

test.describe("Workspaces — the dashboard shell", () => {
  test("open, see the dashboard, reload, charts still there, post a message", async ({ page }) => {
    const { sends, reads } = await setupMocks(page);

    await page.goto(`/?v=chat&room=${ROOM}`, { waitUntil: "networkidle" });

    const view = page.locator('[data-testid="room-view"]');
    await expect(view).toBeVisible({ timeout: 15000 });
    await expect(view.getByRole("heading", { level: 1 })).toHaveText("Weekly sales");
    await expect(page.locator('[data-testid="room-runs-as"]')).toHaveText("Runs as Alice");

    const pinned = page.locator('[data-testid="room-pinned-edition"]');
    await expect(pinned.locator('[data-testid="edition-title"]')).toHaveText("Week 47");
    await expect(pinned.locator('[data-testid="bar-chart"]')).toBeVisible();
    await expect(page.locator('[data-testid="room-past-editions"]')).toContainText("Week 46");
    await expect.poll(() => reads.length).toBeGreaterThan(0);

    // The conversation: authors named, and the edition the agent announced drawn in place.
    const list = page.locator('[data-testid="room-view"] [data-testid="message-list"]');
    await expect(list.getByText("How did we do?")).toBeVisible();
    await expect(list.locator('[data-testid="message-author"]').first()).toHaveText("Bob");
    await expect(list.locator('[data-testid="message-edition"] [data-testid="bar-chart"]')).toBeVisible();

    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="room-pinned-edition"] [data-testid="bar-chart"]')).toBeVisible({ timeout: 15000 });
    await expect(list.locator('[data-testid="message-edition"] [data-testid="bar-chart"]')).toBeVisible();
    await expect(list.locator('[data-testid="message-author"]').nth(1)).toHaveText("Analyst");

    const input = view.locator('[data-testid="chat-input"]');
    await input.fill("And margins?");
    await view.locator('[data-testid="send-button"]').click();
    await expect(list.getByText("Margins held at 31%.")).toBeVisible();
    expect(sends).toHaveLength(1);
    expect(sends[0]).toEqual({ message: "And margins?", request_id: expect.any(String), room: ROOM, client_id: expect.any(String) });

    // And home again: the main conversation, with no room on the request.
    await page.locator('[data-testid="room-rail-main"]').click();
    await expect(page).toHaveURL(/\?v=chat$/);
    await expect(view).toHaveCount(0);
  });

  test("at tablet width the collapsed rail opens on screen and opens a workspace", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 900, height: 1000 });
    await page.goto("/?v=chat", { waitUntil: "networkidle" });

    const toggle = page.locator('[data-testid="room-rail-toggle"]');
    await expect(toggle).toBeVisible({ timeout: 15000 });
    await toggle.click();
    const item = page.locator(`[data-testid="room-rail-item-${ROOM}"]`);
    await expect(item).toBeInViewport();
    await item.click();
    await expect(page.locator('[data-testid="room-view"]')).toBeVisible({ timeout: 15000 });
    await expect(page).toHaveURL(new RegExp(`room=${ROOM}`));
  });
});
