/**
 * The workspaces hub in the real browser:
 * - C1: "New in your workspaces" on the chat landing and the rail; the badge
 *   clears once the workspace is opened.
 * - H1: a workspace link in a chat reply opens it client-side (no page load),
 *   and still works after the history is read again on reload.
 * - E1/E2/E5/E3: the tasks tab: a suggestion linked to its edition, accept
 *   told it was taken, dismiss, and approving my own proposal with a receipt.
 * - E6: "Open as workspace" on a task in the Tasks view.
 * - Inbox: a workspace note links to its workspace; marking it read.
 *
 * Every backend is intercepted with replies shaped like the shared-threads
 * contract and the existing bridge routes; no bridge or engine is needed.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const ROOM = "11111111-1111-4111-8111-111111111111";
const TASK_ROOM = "33333333-3333-4333-8333-333333333333";
const EDITION = "aaaaaaaa-0000-4000-8000-000000000002";
const SUGGESTED = "bbbbbbbb-0000-4000-8000-000000000001";
const TAKEN = "bbbbbbbb-0000-4000-8000-000000000002";
const OTHER = "bbbbbbbb-0000-4000-8000-000000000003";
const CRM_TASK = "bbbbbbbb-0000-4000-8000-000000000009";
const ACTION = "cccccccc-0000-4000-8000-000000000001";
const NOTE = "dddddddd-0000-4000-8000-000000000001";
const ME = "eeeeeeee-0000-4000-8000-00000000000b";

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

const EDITIONS = [
  {
    id: EDITION,
    seq: 2,
    kind: "edition",
    status: "published",
    preview: false,
    periodStart: "2026-11-16",
    periodEnd: "2026-11-22",
    grain: "week",
    revision: 1,
    title: "Week 47",
    summary: "Revenue up on the week.",
    blocks: [{ component: "render_markdown", props: { content: "Strong finish." } }],
    suggestions: [{ key: "lapsed", title: "Call the three lapsed customers" }],
    provenance: { run_id: "run-1", ran_as: "u-alice", started_at: "2026-11-22T08:00:00Z", tool_calls: [] },
    createdAt: "2026-11-22T08:05:00Z",
  },
];

function roomTask(taskId: string, title: string, extra: Record<string, unknown> = {}) {
  return {
    taskId,
    title,
    status: "TODO",
    origin: "suggestion",
    editionId: EDITION,
    assignee: null,
    acceptedBy: null,
    dismissed: false,
    ...extra,
  };
}

async function setupMocks(page: Page) {
  const state = {
    unread: 2,
    mainHistory: [] as Array<Record<string, unknown>>,
    tasks: [
      roomTask(SUGGESTED, "Call the three lapsed customers"),
      roomTask(OTHER, "Reprice the bundle"),
      roomTask(TAKEN, "Send the follow-up emails", {
        status: "IN_PROGRESS",
        assignee: { userId: "u-bob", displayName: "Bob" },
        acceptedBy: { userId: "u-bob", displayName: "Bob" },
      }),
    ],
    pending: [
      {
        kind: "action",
        id: ACTION,
        run_id: "",
        agent_id: "",
        action_kind: "room.email",
        question: "room.email: Following up on your order",
        detail: "Hi Alice,\n\nJust checking the order arrived safely.",
        envelope: { sender: "agent@example.com", recipient: "alice@example.com", subject: "Following up on your order" },
        email: {
          from: "agent@example.com",
          replyTo: "bob@example.com",
          to: ["alice@example.com"],
          cc: [],
          subject: "Following up on your order",
          body: "Hi Alice,\n\nJust checking the order arrived safely.",
          onBehalfOf: "Bob",
        },
        options: [],
        expires_at: "2099-01-01T12:00:00Z",
        created_at: "2026-11-21T09:00:00Z",
        // Member approval lives in the task's own workspace.
        room_id: TASK_ROOM,
        task_id: CRM_TASK,
      },
    ] as Array<Record<string, unknown>>,
    notes: [
      {
        id: NOTE,
        tenantId: "test-tenant",
        fromAgent: "main",
        toAgent: ME,
        notificationType: "info",
        subject: "New edition: Week 47",
        body: "Weekly sales has a new edition.",
        metadata: { room_id: ROOM },
        taskId: null,
        readAt: null as string | null,
        acknowledgedAt: null,
        createdAt: "2026-11-21T08:05:00Z",
      },
    ],
    fromTask: [] as string[],
    answers: [] as unknown[],
    reads: [] as string[],
  };

  const summary = () => ({
    id: ROOM,
    kind: "report",
    title: "Weekly sales",
    agentId: "main",
    paused: false,
    archived: false,
    unread: state.unread,
    lastActivityAt: "2026-11-21T09:00:00Z",
    latestEdition: { id: EDITION, seq: 2, title: "Week 47", periodStart: "2026-11-16", periodEnd: "2026-11-22" },
  });
  const taskRoom = {
    id: TASK_ROOM,
    kind: "task",
    title: "Renew the lease",
    agentId: "main",
    paused: false,
    archived: false,
    unread: 0,
    lastActivityAt: "2026-11-19T09:00:00Z",
  };
  const detail = (base: Record<string, unknown>) => ({
    ...base,
    instructions: "",
    settings: {},
    rev: 1,
    access: "shared",
    members: [
      { userId: "u-alice", displayName: "Alice", assigned: true },
      { userId: "u-bob", displayName: "Bob", assigned: false },
    ],
  });

  await page.route("**/api/bridge/api/agent-manifests", (route) => json(route, { agents: [], default_agent: "main" }));
  await page.route("**/api/chat/plan/status*", (route) => json(route, { active: false }));
  await page.route("**/api/chat/deep/status*", (route) => json(route, { active: false }));
  await page.route("**/api/session", (route) => json(route, {}));
  await page.route("**/api/dashboard/welcome", (route) => json(route, { html: "<div>Welcome</div>", type: "html" }));
  await page.route("**/api/dashboard/generate", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/health", (route) => json(route, { status: "healthy", agents: {} }));
  await page.route("**/api/actions/execute", (route) => {
    const body = route.request().postDataJSON() as { tool?: string } | null;
    const payload =
      body?.tool === "agent_status"
        ? { agents: [{ name: "Main", agentId: "main", schedule: "", status: "healthy" }] }
        : { tasks: [{ id: CRM_TASK, title: "Renew the lease", status: "TODO" }] };
    return json(route, { data: payload });
  });
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
    const room = url.searchParams.get("room");
    return json(route, { messages: room ? [] : state.mainHistory, hasMore: false });
  });
  await page.route("**/api/bridge/api/auth/me", (route) => json(route, { id: ME, role: "owner" }));
  await page.route("**/api/bridge/api/notifications**", (route) => {
    const url = new URL(route.request().url());
    const read = url.pathname.match(/\/notifications\/([^/]+)\/read$/);
    if (read && route.request().method() === "POST") {
      state.notes = state.notes.map((n) => (n.id === read[1] ? { ...n, readAt: "2026-11-21T10:00:00Z" } : n));
      return json(route, { success: true, id: read[1] });
    }
    return json(route, { notifications: state.notes });
  });
  await page.route("**/api/bridge/api/approvals**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/api/approvals")) return json(route, { count: state.pending.length, pending: state.pending });
    if (url.pathname.endsWith(`/action/${ACTION}`) && route.request().method() === "POST") {
      state.answers.push(route.request().postDataJSON());
      state.pending = [];
      return json(route, { settled: true, kind: "action", id: ACTION });
    }
    return json(route, { error: "Not found" }, 404);
  });
  await page.route("**/api/bridge/api/rooms**", (route) => {
    const method = route.request().method();
    const path = new URL(route.request().url()).pathname.replace("/api/bridge/api/rooms", "");
    if (path === "") return json(route, { rooms: [summary(), taskRoom] });
    if (path === `/from-task/${CRM_TASK}` && method === "POST") {
      state.fromTask.push(CRM_TASK);
      return json(route, detail(taskRoom));
    }
    if (path === `/${ROOM}`) return json(route, detail(summary()));
    if (path === `/${TASK_ROOM}`) return json(route, detail(taskRoom));
    if (path.endsWith("/read") && method === "POST") {
      state.reads.push(path);
      state.unread = 0;
      return json(route, { ok: true });
    }
    if (path === `/${ROOM}/editions`) return json(route, { editions: EDITIONS });
    if (path === `/${TASK_ROOM}/editions`) return json(route, { editions: [] });
    if (path === `/${TASK_ROOM}/tasks`) {
      return json(route, { tasks: [roomTask(CRM_TASK, "Renew the lease", { origin: "subject", editionId: null })] });
    }
    if (path === `/${ROOM}/tasks`) return json(route, { tasks: state.tasks });
    const decide = path.match(/^\/[^/]+\/tasks\/([^/]+)\/(accept|dismiss)$/);
    if (decide && method === "POST") {
      const [, id, verb] = decide;
      if (verb === "accept") {
        return json(route, { error: "already accepted", acceptedBy: { userId: "u-alice", displayName: "Alice" } }, 409);
      }
      state.tasks = state.tasks.map((t) => (t.taskId === id ? { ...t, dismissed: true } : t));
      return json(route, state.tasks.find((t) => t.taskId === id));
    }
    return json(route, { error: "Not found" }, 404);
  });
  return state;
}

/** Set once per page load: still there means no load happened in between. */
async function markPage(page: Page) {
  await page.evaluate(() => ((window as unknown as { __sameLoad?: boolean }).__sameLoad = true));
}
async function samePage(page: Page): Promise<boolean> {
  return page.evaluate(() => (window as unknown as { __sameLoad?: boolean }).__sameLoad === true);
}

test.describe("Workspaces — the hub", () => {
  test("new in your workspaces, on the landing and the rail; opening clears the badge (C1)", async ({ page }) => {
    await setupMocks(page);
    await page.goto("/?v=chat", { waitUntil: "networkidle" });
    await markPage(page);

    const landing = page.locator('[data-testid="chat-landing"] [data-testid="new-in-workspaces"]');
    await expect(landing.locator(`[data-testid="new-in-workspaces-item-${ROOM}"]`)).toContainText("Weekly sales", {
      timeout: 15000,
    });
    const rail = page.locator('[data-testid="room-rail"] [data-testid="new-in-workspaces"]');
    await rail.locator('[data-testid="new-in-workspaces-toggle"]').click();
    await expect(rail.locator(`[data-testid="new-in-workspaces-item-${ROOM}"]`)).toBeVisible();
    await expect(page.locator(`[data-testid="room-unread-${ROOM}"]`)).toHaveText("2");

    await landing.locator(`[data-testid="new-in-workspaces-item-${ROOM}"]`).click();
    await expect(page).toHaveURL(new RegExp(`\\?v=chat&room=${ROOM}$`));
    await expect(page.locator('[data-testid="room-view"]')).toBeVisible();
    expect(await samePage(page)).toBe(true);
    await expect(page.locator(`[data-testid="room-unread-${ROOM}"]`)).toHaveCount(0);

    await page.locator('[data-testid="room-rail-main"]').click();
    await expect(rail.locator('[data-testid="new-in-workspaces-empty"]')).toBeVisible();
  });

  test("a workspace link in chat opens it without a page load, and survives a reload (H1)", async ({ page }) => {
    const state = await setupMocks(page);
    state.mainHistory = [
      { id: 1, role: "user", content: "What's new in my workspaces?" },
      { id: 2, role: "assistant", content: `Weekly sales has a new edition: [Weekly sales](/?v=chat&room=${ROOM}).` },
    ];
    await page.goto("/?v=chat", { waitUntil: "networkidle" });
    await markPage(page);

    const link = page.locator('[data-testid="message-list"] a', { hasText: "Weekly sales" });
    await expect(link).toHaveAttribute("href", `/?v=chat&room=${ROOM}`, { timeout: 15000 });
    await link.click();
    await expect(page.locator('[data-testid="room-view"]')).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`room=${ROOM}`));
    expect(await samePage(page)).toBe(true);

    // Back, then a reload reads the history again: the link still works.
    await page.goBack();
    await expect(page.locator('[data-testid="room-view"]')).toHaveCount(0);
    await page.reload({ waitUntil: "networkidle" });
    await markPage(page);
    const again = page.locator('[data-testid="message-list"] a', { hasText: "Weekly sales" });
    await again.click({ timeout: 15000 });
    await expect(page.locator('[data-testid="room-view"]')).toBeVisible();
    expect(await samePage(page)).toBe(true);
  });

  test("the tasks tab: suggestion, taken, dismiss; approving my own email in the task workspace (E1, E2, E5, E3)", async ({ page }) => {
    const state = await setupMocks(page);
    await page.goto(`/?v=chat&room=${ROOM}`, { waitUntil: "networkidle" });

    const tab = page.locator('[data-testid="room-tasks"]');
    await expect(tab).toBeVisible({ timeout: 15000 });
    await expect(tab.locator(`[data-testid="room-task-edition-${SUGGESTED}"]`)).toHaveText(/Week 47/);
    await expect(tab.locator(`[data-testid="room-task-${TAKEN}"]`)).toContainText("Bob");

    await tab.locator(`[data-testid="room-task-accept-${SUGGESTED}"]`).click();
    await expect(tab.locator(`[data-testid="room-task-message-${SUGGESTED}"]`)).toContainText(
      "Alice already took this task",
    );

    await tab.locator(`[data-testid="room-task-dismiss-${OTHER}"]`).click();
    await expect(tab.locator(`[data-testid="room-task-${OTHER}"]`)).toHaveCount(0);

    // A report workspace decides nothing: it leads to the task's own workspace.
    await expect(tab.locator('[data-testid="room-approvals"]')).toHaveCount(0);
    await expect(tab.locator(`[data-testid="open-as-workspace-${TAKEN}"]`)).toContainText("Open task workspace");

    await page.goto(`/?v=chat&room=${TASK_ROOM}`, { waitUntil: "networkidle" });
    const card = page.locator(`[data-testid="room-email-${ACTION}"]`);
    await expect(card).toBeVisible({ timeout: 15000 });
    await expect(card.locator('[data-testid="room-email-to"]')).toHaveText("alice@example.com");
    await expect(card.locator('[data-testid="room-email-reply-to"]')).toHaveText("bob@example.com");
    await expect(card.locator('[data-testid="room-email-body"]')).toContainText("Just checking the order arrived safely.");
    await card.getByRole("button", { name: /approve and send/i }).click();
    await expect(page.locator(`[data-testid="room-approval-receipt-${ACTION}"]`)).toContainText("Approved");
    expect(state.answers).toEqual([{ approved: true }]);
  });

  test("open a task as a workspace from the Tasks view (E6)", async ({ page }) => {
    const state = await setupMocks(page);
    await page.goto("/?v=tasks", { waitUntil: "networkidle" });
    await markPage(page);
    await page.locator(`[data-testid="open-as-workspace-${CRM_TASK}"]`).click({ timeout: 15000 });
    await expect(page).toHaveURL(new RegExp(`\\?v=chat&room=${TASK_ROOM}$`));
    await expect(page.locator('[data-testid="room-view"] h1')).toHaveText("Renew the lease");
    expect(state.fromTask).toEqual([CRM_TASK]);
    expect(await samePage(page)).toBe(true);
  });

  test("the inbox lists a workspace note that opens the workspace, and marks it read", async ({ page }) => {
    await setupMocks(page);
    await page.goto("/?v=inbox", { waitUntil: "networkidle" });
    const notes = page.locator('[data-testid="inbox-notifications"]');
    await expect(notes).toContainText("New edition: Week 47", { timeout: 15000 });
    await notes.locator(`[data-testid="inbox-notification-read-${NOTE}"]`).click();
    await expect(notes.locator(`[data-testid="inbox-notification-read-${NOTE}"]`)).toHaveCount(0);
    await notes.locator(`[data-testid="inbox-notification-open-${NOTE}"]`).click();
    await expect(page).toHaveURL(new RegExp(`room=${ROOM}`));
    await expect(page.locator('[data-testid="room-view"]')).toBeVisible();
  });

  test("works at phone width", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await setupMocks(page);
    await page.goto("/?v=chat", { waitUntil: "networkidle" });
    await expect(
      page.locator(`[data-testid="chat-landing"] [data-testid="new-in-workspaces-item-${ROOM}"]`),
    ).toBeVisible({ timeout: 15000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(overflow).toBe(false);
  });

  for (const [width, height, label] of [
    [390, 844, "phone"],
    [820, 1180, "tablet"],
  ] as const) {
    test(`the collapsed rail closes after opening a workspace from its digest (${label})`, async ({ page }) => {
      await page.setViewportSize({ width, height });
      await setupMocks(page);
      await page.goto("/?v=chat", { waitUntil: "networkidle" });
      const rail = page.locator('[data-testid="room-rail"]');
      await rail.locator('[data-testid="room-rail-toggle"]').click({ timeout: 15000 });
      const nav = rail.getByRole("navigation", { name: "Workspaces" });
      await expect(nav).toBeVisible();
      await nav.locator('[data-testid="new-in-workspaces-toggle"]').click();
      await nav.locator(`[data-testid="new-in-workspaces-item-${ROOM}"]`).click();
      await expect(page).toHaveURL(new RegExp(`room=${ROOM}`));
      await expect(page.locator('[data-testid="room-view"]')).toBeVisible();
      await expect(nav).toHaveCount(0);
    });
  }
});
