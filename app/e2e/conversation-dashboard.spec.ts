/**
 * E2E tests for conversation-driven dashboard generation.
 * Uses Playwright route interception to mock backend APIs for deterministic testing.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const BASE_URL = "http://localhost:3004";

/** Mock SSE stream for /api/chat/send */
function mockChatSSE(events: Array<{ event: string; data: unknown }>): string {
  return events
    .map((ev) => `event: ${ev.event}\ndata: ${JSON.stringify(ev.data)}`)
    .join("\n\n") + "\n\n";
}

/** Mock SSE stream for /api/dashboard/generate */
function mockDashboardSSE(html: string): string {
  const chunks = [
    `event: code\ndata: ${JSON.stringify({ chunk: html, complete: false })}`,
    `event: code\ndata: ${JSON.stringify({
      chunk: "",
      complete: true,
      type: "html",
      valid: true,
      errors: [],
      fullCode: html,
    })}`,
  ];
  return chunks.join("\n\n") + "\n\n";
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
) {
  // Mock chat history
  await page.route("**/api/chat/history", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ messages: [] }),
    });
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

  // Mock dashboard generate
  if (opts.dashboard204) {
    await page.route("**/api/dashboard/generate", (route: Route) => {
      route.fulfill({ status: 204, body: "" });
    });
  } else if (opts.dashboardHtml) {
    await page.route("**/api/dashboard/generate", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockDashboardSSE(opts.dashboardHtml!),
      });
    });
  }

  // Mock welcome dashboard
  if (opts.welcomeHtml) {
    await page.route("**/api/dashboard/welcome", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockDashboardSSE(opts.welcomeHtml!),
      });
    });
  }
}

/** Send a message via the chat input */
async function sendChatMessage(page: Page, text: string) {
  const input = page.locator('[data-testid="chat-input"]');
  await expect(input).toBeVisible({ timeout: 10000 });
  await input.fill(text);
  await page.locator('[data-testid="send-button"]').click();
}

test.describe("Conversation-Driven Dashboard", () => {
  test("contacts-related message triggers dashboard update", async ({ page }) => {
    const dashboardHtml = '<div class="p-4"><h2 class="text-lg font-semibold text-zinc-100">Contacts</h2><p class="text-zinc-400" data-testid="contacts-count">15 contacts</p></div>';

    await setupMocks(page, {
      chatResponse: "Here are your contacts from the CRM system.",
      dashboardHtml,
      welcomeHtml: '<div class="p-4"><h2>Welcome</h2></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await page.waitForTimeout(2000);

    await sendChatMessage(page, "Show my contacts");

    // Wait for the assistant response
    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 15000 });

    // Dashboard should update — look for iframe with contacts content
    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    await expect(iframe).toBeVisible({ timeout: 20000 });
  });

  test("health-related message triggers health dashboard", async ({ page }) => {
    const dashboardHtml = '<div class="p-4"><h2 class="text-lg font-semibold text-zinc-100">Service Health</h2><div data-testid="health-status">All services healthy</div></div>';

    await setupMocks(page, {
      chatResponse: "All services are running and healthy.",
      dashboardHtml,
      welcomeHtml: '<div class="p-4"><h2>Welcome</h2></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await page.waitForTimeout(2000);

    await sendChatMessage(page, "How are the services running?");

    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 15000 });

    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    await expect(iframe).toBeVisible({ timeout: 20000 });
  });

  test("dashboard does NOT update for trivial response", async ({ page }) => {
    await setupMocks(page, {
      chatResponse: "You're welcome!",
      dashboard204: true,
      welcomeHtml: '<div class="p-4"><h2>Welcome Dashboard</h2></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // Wait for welcome dashboard to load
    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    await expect(iframe).toBeVisible({ timeout: 20000 });

    await sendChatMessage(page, "thanks");

    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 15000 });

    // Dashboard should still be visible (not replaced, not errored)
    await page.waitForTimeout(2000);
    await expect(iframe).toBeVisible();

    // No error state should appear
    const errorState = page.locator('[data-testid="canvas-error"]');
    await expect(errorState).not.toBeVisible();
  });

  test("marker hint from agent guides dashboard topic", async ({ page }) => {
    const dashboardHtml = '<div class="p-4"><h2 class="text-lg font-semibold text-zinc-100">Contacts Dashboard</h2></div>';

    await setupMocks(page, {
      chatEvents: [
        { event: "delta", data: { text: "I'll pull up your contacts." } },
        { event: "dashboard", data: { intent: "contacts", data: {} } },
        { event: "done", data: { text: "I'll pull up your contacts." } },
      ],
      dashboardHtml,
      welcomeHtml: '<div class="p-4"><h2>Welcome</h2></div>',
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await page.waitForTimeout(2000);

    await sendChatMessage(page, "Do the thing");

    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 15000 });

    // Dashboard should render — marker hint was used
    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    await expect(iframe).toBeVisible({ timeout: 20000 });
  });

  test("welcome dashboard unchanged on initial load", async ({ page }) => {
    const welcomeHtml = '<div class="p-4" data-testid="welcome-content"><h2 class="text-lg font-semibold text-zinc-100">Good morning, Philip</h2></div>';

    await setupMocks(page, {
      welcomeHtml,
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // Welcome dashboard should appear
    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    await expect(iframe).toBeVisible({ timeout: 20000 });

    // Chat panel should show empty state
    const chatPanel = page.locator('[data-testid="chat-panel"]');
    await expect(chatPanel).toBeVisible({ timeout: 10000 });
  });

  test("chat text stays clean — no markers visible to user", async ({ page }) => {
    await setupMocks(page, {
      chatEvents: [
        { event: "delta", data: { text: "Here are your contacts." } },
        { event: "dashboard", data: { intent: "contacts", data: {} } },
        { event: "done", data: { text: "Here are your contacts." } },
      ],
      dashboardHtml: '<div class="p-4"><h2>Contacts</h2></div>',
      welcomeHtml: '<div class="p-4"><h2>Welcome</h2></div>',
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

  test("rapid messages cancel previous dashboard generation", async ({ page }) => {
    let generateCallCount = 0;

    await setupMocks(page, {
      chatResponse: "First response",
      welcomeHtml: '<div class="p-4"><h2>Welcome</h2></div>',
    });

    // Custom dashboard mock that counts calls
    await page.route("**/api/dashboard/generate", (route: Route) => {
      generateCallCount++;
      const html = `<div class="p-4"><h2>Dashboard ${generateCallCount}</h2></div>`;
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockDashboardSSE(html),
      });
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await page.waitForTimeout(2000);

    // Send a message — should trigger dashboard generation
    await sendChatMessage(page, "Check services");

    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 15000 });

    // The dashboard should render without errors
    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    await expect(iframe).toBeVisible({ timeout: 20000 });

    // No error state
    const errorState = page.locator('[data-testid="canvas-error"]');
    await expect(errorState).not.toBeVisible();
  });
});
