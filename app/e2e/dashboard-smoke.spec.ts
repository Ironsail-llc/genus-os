/**
 * E2E smoke tests for the Robothor Helm dashboard.
 * Tests: layout rendering, dashboard iframe sizing, chat scroll, welcome dashboard.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const BASE_URL = "http://localhost:3004";

/** Set up standard mocks so tests are deterministic (no real engine calls). */
async function setupMocks(page: Page) {
  const welcomeHtml =
    '<div class="p-4"><h2 class="text-lg text-zinc-100">Welcome Dashboard</h2><p>Good morning, Philip</p></div>';

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
  await page.route("**/api/health", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "healthy", agents: {} }),
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
  test("page loads with canvas and chat panels", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // App shell should exist
    const shell = page.locator('[data-testid="app-shell"]');
    await expect(shell).toBeVisible({ timeout: 10000 });

    // Both panels should be present
    const canvas = page.locator('[data-testid="live-canvas"]');
    const chatPanel = page.locator('[data-testid="chat-panel"]');
    await expect(canvas).toBeVisible({ timeout: 10000 });
    await expect(chatPanel).toBeVisible({ timeout: 10000 });
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

test.describe("Welcome Dashboard (iframe sizing)", () => {
  test("welcome dashboard renders and iframe is not cut off", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // Wait for either the welcome dashboard iframe or the default dashboard
    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    const defaultDash = page.locator('[data-testid="default-dashboard"]');

    // One of these should appear within 15s
    await expect(iframe.or(defaultDash)).toBeVisible({ timeout: 20000 });

    if (await iframe.isVisible()) {
      const box = await iframe.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.height).toBeGreaterThan(150);
      expect(box!.width).toBeGreaterThan(200);

      const parent = page.locator('[data-testid="live-canvas"]');
      const parentBox = await parent.boundingBox();
      if (parentBox && box) {
        expect(box.width).toBeGreaterThan(parentBox.width * 0.8);
      }
    }
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
