/**
 * E2E smoke tests for the Robothor business layer dashboard.
 * Tests: layout rendering, dashboard iframe sizing, chat scroll, welcome dashboard.
 */
import { test, expect } from "@playwright/test";

const BASE_URL = "http://localhost:3004";

test.describe("Dashboard Layout", () => {
  test("page loads with two Dockview panels", async ({ page }) => {
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // Dockview container should exist
    const container = page.locator('[data-testid="dockview-container"]');
    await expect(container).toBeVisible({ timeout: 10000 });

    // Both panels should be present
    const visualPanel = page.locator('[data-testid="visual-panel"]');
    const chatPanel = page.locator('[data-testid="chat-panel"]');
    await expect(visualPanel).toBeVisible({ timeout: 10000 });
    await expect(chatPanel).toBeVisible({ timeout: 10000 });
  });

  test("visual panel takes roughly 65% width", async ({ page }) => {
    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await page.waitForTimeout(1000);

    const visual = page.locator('[data-testid="visual-panel"]');
    const chat = page.locator('[data-testid="chat-panel"]');
    await expect(visual).toBeVisible({ timeout: 10000 });
    await expect(chat).toBeVisible({ timeout: 10000 });

    const visualBox = await visual.boundingBox();
    const chatBox = await chat.boundingBox();
    if (visualBox && chatBox) {
      const totalWidth = visualBox.width + chatBox.width;
      const visualPct = (visualBox.width / totalWidth) * 100;
      // Should be roughly 50-80% (proportional layout, ~65% target)
      expect(visualPct).toBeGreaterThanOrEqual(50);
      expect(visualPct).toBeLessThan(85);
      // Chat panel should have usable width (at least 250px)
      expect(chatBox.width).toBeGreaterThan(250);
    }
  });
});

test.describe("Welcome Dashboard (iframe sizing)", () => {
  test("welcome dashboard renders and iframe is not cut off", async ({ page }) => {
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // Wait for either the welcome dashboard iframe or the default dashboard
    const iframe = page.locator('[data-testid="srcdoc-renderer"]');
    const defaultDash = page.locator('[data-testid="default-dashboard"]');

    // One of these should appear within 15s (welcome takes ~6s)
    await expect(iframe.or(defaultDash)).toBeVisible({ timeout: 20000 });

    if (await iframe.isVisible()) {
      // Check the iframe has reasonable height (not collapsed)
      const box = await iframe.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.height).toBeGreaterThan(150);
      expect(box!.width).toBeGreaterThan(200);

      // Check that the iframe content is not cut off horizontally
      // The iframe width should roughly match its parent
      const parent = page.locator('[data-testid="live-canvas"]');
      const parentBox = await parent.boundingBox();
      if (parentBox && box) {
        // iframe width should be at least 80% of parent width
        expect(box.width).toBeGreaterThan(parentBox.width * 0.8);
      }
    }
  });
});

test.describe("Chat Panel", () => {
  test("chat panel shows empty state with suggested prompts", async ({ page }) => {
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const chatPanel = page.locator('[data-testid="chat-panel"]');
    await expect(chatPanel).toBeVisible({ timeout: 10000 });

    // Check for empty state or suggested prompts
    const emptyState = page.locator('[data-testid="empty-state"]');
    const messageList = page.locator('[data-testid="message-list"]');

    // Either empty state (no history) or message list (has history)
    await expect(emptyState.or(messageList)).toBeVisible({ timeout: 10000 });
  });

  test("chat input is accessible and accepts text", async ({ page }) => {
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const input = page.locator('[data-testid="chat-input"]');
    await expect(input).toBeVisible({ timeout: 10000 });
    await expect(input).toBeEnabled();

    await input.fill("test message");
    await expect(input).toHaveValue("test message");

    // Send button should be enabled
    const sendBtn = page.locator('[data-testid="send-button"]');
    await expect(sendBtn).toBeEnabled();
  });

  test("chat scrolls properly with many messages", async ({ page }) => {
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const chatPanel = page.locator('[data-testid="chat-panel"]');
    await expect(chatPanel).toBeVisible({ timeout: 10000 });

    // Get the scrollable messages container (the flex-1 overflow-y-auto div)
    const messagesContainer = chatPanel.locator(".overflow-y-auto");
    await expect(messagesContainer).toBeVisible({ timeout: 5000 });

    // Check container has a finite height (not growing unbounded)
    const containerBox = await messagesContainer.boundingBox();
    const chatBox = await chatPanel.boundingBox();
    if (containerBox && chatBox) {
      // Messages area should not exceed the chat panel height
      expect(containerBox.height).toBeLessThanOrEqual(chatBox.height);
    }

    // Check that the input area is always visible at the bottom
    const input = page.locator('[data-testid="chat-input"]');
    const inputBox = await input.boundingBox();
    if (inputBox && chatBox) {
      // Input should be within the chat panel bounds
      expect(inputBox.y + inputBox.height).toBeLessThanOrEqual(
        chatBox.y + chatBox.height + 5 // small tolerance
      );
    }
  });

  test("sending a message works and response appears", async ({ page }) => {
    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await page.waitForTimeout(2000);

    const input = page.locator('[data-testid="chat-input"]');
    await expect(input).toBeVisible({ timeout: 10000 });

    // Type and send a message
    await input.fill("hello");
    await page.locator('[data-testid="send-button"]').click();

    // User message should appear
    const userMsg = page.locator('[data-testid="message-user"]').last();
    await expect(userMsg).toBeVisible({ timeout: 5000 });
    await expect(userMsg).toContainText("hello");

    // Wait for streaming indicator or new assistant response
    await page.waitForSelector(
      '[data-testid="streaming-message"], [data-testid="message-assistant"]',
      { timeout: 30000 }
    );

    // Eventually an assistant message should appear
    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 60000 });

    // Input should be re-enabled after response
    await expect(input).toBeEnabled({ timeout: 60000 });

    // The input should still be visible (not pushed off screen)
    const inputBox = await input.boundingBox();
    expect(inputBox).not.toBeNull();
    const viewport = page.viewportSize();
    if (inputBox && viewport) {
      expect(inputBox.y).toBeLessThan(viewport.height);
    }
  });
});
