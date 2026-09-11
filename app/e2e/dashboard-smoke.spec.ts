/**
 * E2E smoke tests for the Robothor Helm dashboard.
 *
 * Home is the real-data dashboard: health, tasks and agents read from the BFF,
 * with no model call on load. The generated canvas is behind an explicit
 * control, so the iframe-sizing checks run after that control is used.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const BASE_URL = "/";

/** Open the AI canvas from Home and wait for the generated view to render. */
async function openAiCanvas(page: Page) {
  await page.locator('[data-testid="dashboard-generate-ai"]').click();
  await expect(page.locator('[data-testid="live-canvas"]')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('[data-testid="srcdoc-renderer"]')).toBeVisible({ timeout: 20000 });
}

/** Set up standard mocks so tests are deterministic (no real engine calls). */
async function setupMocks(page: Page) {
  const welcomeHtml =
    '<div class="p-4"><h2 class="text-lg text-zinc-100">Welcome Dashboard</h2><p>Good morning</p></div>';

  await page.route("**/api/chat/history", (route: Route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ messages: [] }) });
  });
  await page.route("**/api/chat/plan/status", (route: Route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ active: false, plan: null }) });
  });
  await page.route("**/api/chat/deep/status", (route: Route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ active: false, deep: null }) });
  });
  await page.route("**/api/session", (route: Route) => {
    if (route.request().method() === "GET") {
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({}) });
    } else {
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
    }
  });
  await page.route("**/api/dashboard/welcome", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ html: welcomeHtml, type: "html" }),
    });
  });
  await page.route("**/api/dashboard/generate", (route: Route) => {
    route.fulfill({ status: 204, body: "" });
  });
  // The real /api/health shape — Home reads `services` to fill its tile.
  await page.route("**/api/health", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        services: [
          { name: "bridge", status: "healthy" },
          { name: "orchestrator", status: "healthy" },
        ],
        timestamp: new Date().toISOString(),
      }),
    });
  });
  // Tasks and agents both come through the action executor.
  await page.route("**/api/actions/execute", (route: Route) => {
    const body = route.request().postDataJSON() as { tool?: string } | null;
    const payload =
      body?.tool === "agent_status"
        ? { agents: [{ name: "worker", schedule: "hourly", status: "healthy" }] }
        : { tasks: [{ id: "t1", title: "A task", status: "TODO" }] };
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ data: payload }),
    });
  });
  await page.route("**/api/events/stream*", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: "event: ping\ndata: {}\n\n",
    });
  });
}

test.describe("Dashboard Layout", () => {
  test("page loads with the real-data dashboard and chat panels", async ({ page }) => {
    let welcomeCalls = 0;
    await setupMocks(page);
    page.on("request", (req) => {
      if (req.url().includes("/api/dashboard/welcome")) welcomeCalls++;
    });
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // App shell should exist
    const shell = page.locator('[data-testid="app-shell"]');
    await expect(shell).toBeVisible({ timeout: 10000 });

    // Home is the real-data dashboard, beside the chat panel.
    const home = page.locator('[data-testid="default-dashboard"]');
    const chatPanel = page.locator('[data-testid="chat-panel"]');
    await expect(home).toBeVisible({ timeout: 10000 });
    await expect(chatPanel).toBeVisible({ timeout: 10000 });

    // Its sections carry real values, not empty cards.
    const metrics = page.locator('[data-testid="metric-summary"]');
    await expect(metrics).toContainText("2/2");
    await expect(metrics).toContainText("Active Tasks");
    await expect(metrics).toContainText("Agents Online");
    await expect(page.locator('[data-testid="service-health"]')).toBeVisible();
    await expect(page.locator('[data-testid="quick-action"]').first()).toBeVisible();

    // No generated view, and no model call, on load.
    await expect(page.locator('[data-testid="live-canvas"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="srcdoc-renderer"]')).toHaveCount(0);
    expect(welcomeCalls).toBe(0);
  });

  test("the AI control mounts the generated canvas on click", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible({ timeout: 10000 });

    await openAiCanvas(page);

    // And the operator can get back to the real-data home.
    await page.locator('[data-testid="dashboard-close-ai"]').click();
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible();
  });

  test("chat container has usable width", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const chatContainer = page.locator('[data-testid="chat-container"]');
    await expect(chatContainer).toBeVisible({ timeout: 10000 });

    const chatBox = await chatContainer.boundingBox();
    if (chatBox) {
      // Chat panel should have usable width (at least 300px)
      expect(chatBox.width).toBeGreaterThan(300);
    }
  });
});

test.describe("Generated canvas (iframe sizing)", () => {
  test("generated dashboard renders and iframe is not cut off", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="default-dashboard"]')).toBeVisible({ timeout: 10000 });

    // Sizing only means anything once the canvas is on screen, and reaching it
    // is now an explicit act.
    await openAiCanvas(page);

    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    const box = await iframe.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.height).toBeGreaterThan(150);
    expect(box!.width).toBeGreaterThan(200);

    const parentBox = await page.locator('[data-testid="live-canvas"]').boundingBox();
    expect(parentBox).not.toBeNull();
    expect(box!.width).toBeGreaterThan(parentBox!.width * 0.8);
  });
});

test.describe("Chat Panel", () => {
  test("chat panel shows empty state with suggested prompts", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const chatPanel = page.locator('[data-testid="chat-panel"]');
    await expect(chatPanel).toBeVisible({ timeout: 10000 });

    // empty-state is nested inside message-list, so just check either one exists
    const emptyState = page.locator('[data-testid="empty-state"]');
    const messageList = page.locator('[data-testid="message-list"]');
    await expect(emptyState.or(messageList).first()).toBeVisible({ timeout: 10000 });
  });

  test("chat input is accessible and accepts text", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const input = page.locator('[data-testid="chat-input"]');
    await expect(input).toBeVisible({ timeout: 10000 });
    await expect(input).toBeEnabled();

    await input.fill("test message");
    await expect(input).toHaveValue("test message");

    const sendBtn = page.locator('[data-testid="send-button"]');
    await expect(sendBtn).toBeEnabled();
  });

  test("chat scrolls properly with many messages", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const chatPanel = page.locator('[data-testid="chat-panel"]');
    await expect(chatPanel).toBeVisible({ timeout: 10000 });

    const messagesContainer = chatPanel.locator(".overflow-y-auto");
    await expect(messagesContainer).toBeVisible({ timeout: 5000 });

    const containerBox = await messagesContainer.boundingBox();
    const chatBox = await chatPanel.boundingBox();
    if (containerBox && chatBox) {
      expect(containerBox.height).toBeLessThanOrEqual(chatBox.height);
    }

    const input = page.locator('[data-testid="chat-input"]');
    const inputBox = await input.boundingBox();
    if (inputBox && chatBox) {
      expect(inputBox.y + inputBox.height).toBeLessThanOrEqual(
        chatBox.y + chatBox.height + 5
      );
    }
  });
});
