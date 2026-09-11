/**
 * E2E tests for conversation-driven dashboard generation.
 *
 * Home is the real-data dashboard and the generated canvas mounts only when
 * the operator asks for it — so a chat reply regenerates a canvas only while
 * that view is open. These specs pin both halves of that: silence by default,
 * regeneration once the AI view is up.
 *
 * Uses Playwright route interception to mock backend APIs for deterministic testing.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const BASE_URL = "/";

/** Mock SSE stream for /api/chat/send */
function mockChatSSE(events: Array<{ event: string; data: unknown }>): string {
  return events
    .map((ev) => `event: ${ev.event}\ndata: ${JSON.stringify(ev.data)}`)
    .join("\n\n") + "\n\n";
}

/**
 * Records every POST to /api/dashboard/generate. The route is always
 * installed, so "no regeneration" is a counted zero rather than the absence
 * of a visible change.
 */
interface GenerateLog {
  count: number;
  bodies: Array<Record<string, unknown>>;
}

/** Set up route interceptors for a test */
async function setupMocks(
  page: Page,
  opts: {
    chatResponse?: string;
    chatEvents?: Array<{ event: string; data: unknown }>;
    dashboardHtml?: string;
    dashboard204?: boolean;
    welcomeHtml?: string;
  }
): Promise<GenerateLog> {
  const generateLog: GenerateLog = { count: 0, bodies: [] };

  // Mock chat history
  await page.route("**/api/chat/history", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ messages: [] }),
    });
  });

  // Mock plan/deep status (loaded on mount)
  await page.route("**/api/chat/plan/status", (route: Route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ active: false, plan: null }) });
  });
  await page.route("**/api/chat/deep/status", (route: Route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ active: false, deep: null }) });
  });

  // Mock chat send
  if (opts.chatEvents || opts.chatResponse) {
    const events = opts.chatEvents || [
      { event: "delta", data: { text: opts.chatResponse || "" } },
      { event: "done", data: { text: opts.chatResponse || "" } },
    ];
    await page.route("**/api/chat/send", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockChatSSE(events),
      });
    });
  }

  // Mock dashboard generate. The client reads this as buffered JSON, the same
  // shape the real route returns.
  await page.route("**/api/dashboard/generate", (route: Route) => {
    generateLog.count++;
    generateLog.bodies.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
    if (opts.dashboard204 || !opts.dashboardHtml) {
      route.fulfill({ status: 204, body: "" });
      return;
    }
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ html: opts.dashboardHtml, type: "html" }),
    });
  });

  // Mock welcome dashboard (JSON response — live-canvas reads as JSON.parse)
  await page.route("**/api/dashboard/welcome", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        html: opts.welcomeHtml ?? '<div class="p-4"><h2>Welcome</h2></div>',
        type: "html",
      }),
    });
  });

  // Mock session restore (no saved dashboard)
  await page.route("**/api/session", (route: Route) => {
    if (route.request().method() === "GET") {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({}),
      });
    } else {
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
    }
  });

  // Mock events stream (prevents networkidle timeout from open SSE)
  await page.route("**/api/events/stream*", (route: Route) => {
    route.abort();
  });

  // Mock health — the real /api/health shape, which Home reads directly.
  await page.route("**/api/health", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        services: [{ name: "bridge", status: "healthy" }],
        timestamp: new Date().toISOString(),
      }),
    });
  });

  // Tasks and agents on Home both come through the action executor.
  await page.route("**/api/actions/execute", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: { tasks: [], agents: [] } }),
    });
  });

  return generateLog;
}

/**
 * A reply long enough to clear the chat panel's cost guard: replies under 200
 * characters that carry no agent data never reach the generate pipeline at
 * all (chat-panel.tsx), so a spec about regeneration has to send something the
 * product would actually regenerate for.
 */
const SUBSTANTIVE_CONTACTS_REPLY =
  "Here are your contacts from the CRM. There are fifteen people on file, eight of them with a " +
  "company attached and four with no email address recorded. The most recently updated record " +
  "changed two days ago, and three contacts have open tasks assigned against them right now.";

const SUBSTANTIVE_SERVICES_REPLY =
  "All services are running. The bridge, the orchestrator and the vision service each answered " +
  "their health probe on the first attempt, with no restarts recorded in the last day. Queue " +
  "depth is flat, and nothing has been retried since the most recent deploy finished.";

/** Send a message via the chat input */
async function sendChatMessage(page: Page, text: string) {
  const input = page.locator('[data-testid="chat-input"]');
  await expect(input).toBeVisible({ timeout: 10000 });
  await input.fill(text);
  await page.locator('[data-testid="send-button"]').click();
}

/** Wait for the reply to land, so a "nothing happened" assertion has had time to be wrong. */
async function waitForReply(page: Page) {
  await expect(page.locator('[data-testid="message-assistant"]').last()).toBeVisible({
    timeout: 15000,
  });
  // The dashboard agent debounces 300ms before it would fire.
  await page.waitForTimeout(2000);
}

/** Open the AI canvas from Home and wait for the generated view. */
async function openAiCanvas(page: Page) {
  await page.locator('[data-testid="dashboard-generate-ai"]').click();
  await expect(page.locator('[data-testid="live-canvas"]')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('[data-testid="srcdoc-renderer"]')).toBeVisible({ timeout: 20000 });
}

test.describe("Conversation-Driven Dashboard", () => {
  test("a contacts-related message regenerates nothing while the AI view is closed", async ({ page }) => {
    const generate = await setupMocks(page, {
      // Substantive on purpose: this reply *would* regenerate if anything were
      // listening, so the zero below is about the closed view, not the guard.
      chatResponse: SUBSTANTIVE_CONTACTS_REPLY,
      dashboardHtml: '<div class="p-4"><h2>Contacts</h2><p>15 contacts</p></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible({ timeout: 10000 });

    await sendChatMessage(page, "Show my contacts");
    await waitForReply(page);

    expect(generate.count).toBe(0);
    await expect(page.locator('[data-testid="srcdoc-renderer"]')).toHaveCount(0);
    // Home is untouched by the conversation.
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible();
  });

  test("a health-related message regenerates nothing while the AI view is closed", async ({ page }) => {
    const generate = await setupMocks(page, {
      chatResponse: SUBSTANTIVE_SERVICES_REPLY,
      dashboardHtml: '<div class="p-4"><h2>Service Health</h2></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible({ timeout: 10000 });

    await sendChatMessage(page, "How are the services running?");
    await waitForReply(page);

    expect(generate.count).toBe(0);
    await expect(page.locator('[data-testid="srcdoc-renderer"]')).toHaveCount(0);
  });

  test("no dashboard is generated on load — the AI control generates it on request", async ({ page }) => {
    let welcomeCalls = 0;
    await setupMocks(page, {
      welcomeHtml: '<div class="p-4" data-testid="welcome-content"><h2>Good morning</h2></div>',
    });
    page.on("request", (req) => {
      if (req.url().includes("/api/dashboard/welcome")) welcomeCalls++;
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible({ timeout: 10000 });
    expect(welcomeCalls).toBe(0);

    await openAiCanvas(page);
    expect(welcomeCalls).toBeGreaterThan(0);
    await expect(page.locator('[data-testid="srcdoc-renderer"]')).toHaveAttribute(
      "srcdoc",
      /Good morning/,
    );

    // Chat panel is up throughout.
    await expect(page.locator('[data-testid="chat-panel"]')).toBeVisible({ timeout: 10000 });
  });

  test("with the AI view open, a contacts-related message regenerates the canvas", async ({ page }) => {
    const generate = await setupMocks(page, {
      chatResponse: SUBSTANTIVE_CONTACTS_REPLY,
      dashboardHtml:
        '<div class="p-4"><h2>Contacts</h2><p data-testid="contacts-count">15 contacts</p></div>',
      welcomeHtml: '<div class="p-4"><h2>Welcome</h2></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible({ timeout: 10000 });
    await openAiCanvas(page);

    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    await expect(iframe).toHaveAttribute("srcdoc", /Welcome/);

    await sendChatMessage(page, "Show my contacts");
    await waitForReply(page);

    expect(generate.count).toBeGreaterThan(0);
    // The generated view replaced the welcome one — not merely "an iframe exists".
    await expect(iframe).toHaveAttribute("srcdoc", /15 contacts/, { timeout: 20000 });
  });

  test("dashboard does NOT update for trivial response", async ({ page }) => {
    const generate = await setupMocks(page, {
      chatResponse: "You're welcome!",
      dashboard204: true,
      welcomeHtml: '<div class="p-4"><h2>Welcome Dashboard</h2></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible({ timeout: 10000 });
    await openAiCanvas(page);

    const iframe = page.locator('[data-testid="srcdoc-renderer"]');

    await sendChatMessage(page, "thanks");
    await waitForReply(page);

    // A short reply with no agent data is dropped by the chat panel's cost
    // guard before the server's triage ever sees it — so the request is not
    // merely answered 204, it is never made.
    expect(generate.count).toBe(0);

    // The welcome dashboard is still the one on screen, and nothing errored.
    await expect(iframe).toBeVisible();
    await expect(iframe).toHaveAttribute("srcdoc", /Welcome Dashboard/);
    await expect(page.locator('[data-testid="canvas-error"]')).not.toBeVisible();
  });

  test("marker hint from agent guides dashboard topic", async ({ page }) => {
    const generate = await setupMocks(page, {
      chatEvents: [
        { event: "delta", data: { text: "I'll pull up your contacts." } },
        { event: "dashboard", data: { intent: "contacts", data: { contacts: { count: 15 } } } },
        { event: "done", data: { text: "I'll pull up your contacts." } },
      ],
      dashboardHtml: '<div class="p-4"><h2>Contacts Dashboard</h2></div>',
      welcomeHtml: '<div class="p-4"><h2>Welcome</h2></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible({ timeout: 10000 });
    await openAiCanvas(page);

    await sendChatMessage(page, "Do the thing");
    await waitForReply(page);

    // The agent's data rode along with the request, so the model is not asked
    // to re-fetch what the agent already had.
    expect(generate.count).toBeGreaterThan(0);
    expect(JSON.stringify(generate.bodies)).toContain("contacts");
    await expect(page.locator('[data-testid="srcdoc-renderer"]')).toHaveAttribute(
      "srcdoc",
      /Contacts Dashboard/,
      { timeout: 20000 },
    );
  });

  test("chat text stays clean — no markers visible to user", async ({ page }) => {
    await setupMocks(page, {
      chatEvents: [
        { event: "delta", data: { text: "Here are your contacts." } },
        { event: "dashboard", data: { intent: "contacts", data: {} } },
        { event: "done", data: { text: "Here are your contacts." } },
      ],
      dashboardHtml: '<div class="p-4"><h2>Contacts</h2></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await page.waitForTimeout(2000);

    await sendChatMessage(page, "Show contacts");

    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 15000 });

    // The text should be clean — no [DASHBOARD:...] markers
    const msgText = await assistantMsg.textContent();
    expect(msgText).not.toContain("[DASHBOARD:");
    expect(msgText).not.toContain("[RENDER:");
    expect(msgText).toContain("Here are your contacts");
  });

  test("rapid messages leave one coherent dashboard, not an error", async ({ page }) => {
    await setupMocks(page, {
      chatResponse: SUBSTANTIVE_SERVICES_REPLY,
      dashboardHtml: '<div class="p-4"><h2>Services</h2></div>',
      welcomeHtml: '<div class="p-4"><h2>Welcome</h2></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible({ timeout: 10000 });
    await openAiCanvas(page);

    await sendChatMessage(page, "Check services");
    await waitForReply(page);

    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    await expect(iframe).toBeVisible({ timeout: 20000 });
    await expect(iframe).toHaveAttribute("srcdoc", /Services/, { timeout: 20000 });
    await expect(page.locator('[data-testid="canvas-error"]')).not.toBeVisible();
  });
});
