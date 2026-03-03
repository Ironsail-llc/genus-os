/**
 * E2E tests for /deep mode in the Helm UI.
 * Uses Playwright route interception to mock backend APIs for deterministic testing.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const BASE_URL = "http://localhost:3004";

/** Build a mock SSE stream from event objects */
function buildSSE(events: Array<{ event: string; data: unknown }>): string {
  return (
    events
      .map((ev) => `event: ${ev.event}\ndata: ${JSON.stringify(ev.data)}`)
      .join("\n\n") + "\n\n"
  );
}

/** Set up route interceptors common to all deep mode tests */
async function setupMocks(page: Page) {
  const welcomeHtml = '<div class="p-4"><h2 class="text-lg text-zinc-100">Welcome</h2></div>';

  // Mock chat history
  await page.route("**/api/chat/history", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ messages: [] }),
    });
  });

  // Mock session restore (returns no saved html → triggers welcome)
  await page.route("**/api/session", (route: Route) => {
    if (route.request().method() === "GET") {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({}),
      });
    } else {
      // POST — session save (no-op)
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
    }
  });

  // Mock welcome dashboard (JSON response, not SSE)
  await page.route("**/api/dashboard/welcome", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ html: welcomeHtml, type: "html" }),
    });
  });

  // Mock dashboard generate (no-op, 204)
  await page.route("**/api/dashboard/generate", (route: Route) => {
    route.fulfill({ status: 204, body: "" });
  });

  // Mock deep status
  await page.route("**/api/chat/deep/status", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ active: false, deep: null }),
    });
  });

  // Mock plan status
  await page.route("**/api/chat/plan/status", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ active: false, plan: null }),
    });
  });

  // Mock events stream
  await page.route("**/api/events/stream*", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: "event: ping\ndata: {}\n\n",
    });
  });

  // Mock health
  await page.route("**/api/health", (route: Route) => {
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "healthy", agents: {} }),
    });
  });
}

test.describe("Deep Mode", () => {
  test("deep toggle button is visible and toggles deep mode", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // Deep toggle button should be visible
    const deepToggle = page.locator('[data-testid="deep-toggle"]');
    await expect(deepToggle).toBeVisible({ timeout: 10000 });

    // Deep mode badge should NOT be visible initially
    const deepBadge = page.locator('[data-testid="deep-mode-badge"]');
    await expect(deepBadge).not.toBeVisible();

    // Click the toggle → deep mode activates (also activates plan mode)
    await deepToggle.click();
    await expect(deepBadge).toBeVisible({ timeout: 5000 });
    await expect(deepBadge).toContainText("Deep Plan");

    // Input placeholder should change
    const input = page.locator('[data-testid="chat-input"]');
    await expect(input).toHaveAttribute("placeholder", "Ask a deep reasoning question...");

    // Click again → deep mode deactivates
    await deepToggle.click();
    await expect(deepBadge).not.toBeVisible();
    await expect(input).toHaveAttribute("placeholder", "Ask me anything...");
  });

  test("Ctrl+Shift+D keyboard shortcut toggles deep mode", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const deepBadge = page.locator('[data-testid="deep-mode-badge"]');
    const input = page.locator('[data-testid="chat-input"]');
    await expect(input).toBeVisible({ timeout: 10000 });
    await expect(deepBadge).not.toBeVisible();

    // Ctrl+Shift+D → activate (must use uppercase D to match e.key === "D")
    await page.keyboard.press("Control+Shift+KeyD");
    await expect(deepBadge).toBeVisible({ timeout: 5000 });

    // Ctrl+Shift+D → deactivate
    await page.keyboard.press("Control+Shift+KeyD");
    await expect(deepBadge).not.toBeVisible();
  });

  test("sending a message in deep mode shows result with cost badge", async ({ page }) => {
    await setupMocks(page);

    // Mock plan/start → returns deep plan SSE
    await page.route("**/api/chat/plan/start", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockPlanSSE("1. Research the question", "plan-life-001", true),
      });
    });

    // Mock plan/approve → returns deep result SSE
    await page.route("**/api/chat/plan/approve", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockDeepApproveSSE("The meaning of life is 42.", 23.5, 0.87),
      });
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // Activate deep mode
    const deepToggle = page.locator('[data-testid="deep-toggle"]');
    await expect(deepToggle).toBeVisible({ timeout: 10000 });
    await deepToggle.click();
    const deepBadge = page.locator('[data-testid="deep-mode-badge"]');
    await expect(deepBadge).toBeVisible({ timeout: 5000 });

    // Type and send a message → routes through plan flow
    const input = page.locator('[data-testid="chat-input"]');
    await input.fill("What is the meaning of life?");
    await page.locator('[data-testid="send-button"]').click();

    // Plan card should appear first
    const planCard = page.locator('[data-testid="plan-card"]');
    await expect(planCard).toBeVisible({ timeout: 15000 });

    // Approve the plan → triggers deep reasoning
    await page.locator('[data-testid="plan-approve"]').click();

    // Wait for the assistant response
    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 15000 });

    // Check response text includes the answer and the cost
    await expect(assistantMsg).toContainText("The meaning of life is 42.");
    await expect(assistantMsg).toContainText("RLM: 23.5s / $0.87");
  });

  test("plan toggle clears deep mode, deep toggle activates plan", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const deepToggle = page.locator('[data-testid="deep-toggle"]');
    const planToggle = page.locator('[data-testid="plan-toggle"]');
    await expect(deepToggle).toBeVisible({ timeout: 10000 });

    const deepBadge = page.locator('[data-testid="deep-mode-badge"]');
    const planBadge = page.locator('[data-testid="plan-mode-badge"]');

    // Activate deep mode → also activates plan (shown as "Deep Plan" badge)
    await deepToggle.click();
    await expect(deepBadge).toBeVisible({ timeout: 5000 });
    await expect(deepBadge).toContainText("Deep Plan");
    // Plan-only badge is hidden when deep is active
    await expect(planBadge).not.toBeVisible();

    // Click plan toggle → clears deep mode and plan mode
    await planToggle.click();
    await expect(deepBadge).not.toBeVisible();
    await expect(planBadge).not.toBeVisible();

    // Click plan toggle again → activates plan-only mode
    await planToggle.click();
    await expect(planBadge).toBeVisible({ timeout: 5000 });
    await expect(planBadge).toHaveText("Plan Mode");
    await expect(deepBadge).not.toBeVisible();

    // Click deep toggle → activates deep (which includes plan)
    await deepToggle.click();
    await expect(deepBadge).toBeVisible({ timeout: 5000 });
    await expect(planBadge).not.toBeVisible();
  });

  test("deep mode error shows error in response", async ({ page }) => {
    await setupMocks(page);

    // Mock plan/start → returns deep plan
    await page.route("**/api/chat/plan/start", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockPlanSSE("Research plan...", "plan-err-001", true),
      });
    });

    // Mock plan/approve → returns deep error
    await page.route("**/api/chat/plan/approve", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: buildSSE([
          { event: "deep_start", data: { deep_id: "dp-err", query: "test" } },
          { event: "error", data: { error: "RLM service unavailable" } },
          { event: "done", data: { text: "" } },
        ]),
      });
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // Activate deep mode and send
    const deepToggle = page.locator('[data-testid="deep-toggle"]');
    await expect(deepToggle).toBeVisible({ timeout: 10000 });
    await deepToggle.click();

    const input = page.locator('[data-testid="chat-input"]');
    await input.fill("This will fail");
    await page.locator('[data-testid="send-button"]').click();

    // Plan card should appear first
    const planCard = page.locator('[data-testid="plan-card"]');
    await expect(planCard).toBeVisible({ timeout: 15000 });

    // Approve → triggers deep reasoning which fails
    await page.locator('[data-testid="plan-approve"]').click();

    // Wait for the error response
    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 15000 });
    await expect(assistantMsg).toContainText("RLM service unavailable");
  });
});

// ─── Deep Plan Mode Tests ────────────────────────────────────────────

/** Mock plan/start returning a plan SSE */
function mockPlanSSE(planText: string, planId: string, deepPlan = false): string {
  return buildSSE([
    { event: "delta", data: { text: planText } },
    {
      event: "plan",
      data: {
        plan_id: planId,
        plan_text: planText,
        original_message: "test query",
        status: "pending",
        deep_plan: deepPlan,
      },
    },
    { event: "done", data: { text: planText, plan_id: planId } },
  ]);
}

/** Mock plan/approve returning deep reasoning SSE */
function mockDeepApproveSSE(response: string, time_s: number, cost: number): string {
  return buildSSE([
    { event: "deep_start", data: { deep_id: "dp-001", query: "test" } },
    { event: "deep_progress", data: { elapsed_s: 5, status: "running" } },
    { event: "deep_result", data: { response, execution_time_s: time_s, cost_usd: cost } },
    { event: "done", data: { text: response, execution_time_s: time_s, cost_usd: cost } },
  ]);
}

test.describe("Deep Plan Mode", () => {
  test("deep toggle activates plan mode simultaneously", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const deepToggle = page.locator('[data-testid="deep-toggle"]');
    const planToggle = page.locator('[data-testid="plan-toggle"]');
    await expect(deepToggle).toBeVisible({ timeout: 10000 });

    const deepBadge = page.locator('[data-testid="deep-mode-badge"]');
    const planBadge = page.locator('[data-testid="plan-mode-badge"]');

    // Click deep toggle → should show "Deep Plan" badge (combines both modes)
    await deepToggle.click();
    await expect(deepBadge).toBeVisible({ timeout: 5000 });
    await expect(deepBadge).toContainText("Deep Plan");

    // Plan-only badge should NOT be visible when deep is active
    await expect(planBadge).not.toBeVisible();

    // Deep toggle off → both badges disappear
    await deepToggle.click();
    await expect(deepBadge).not.toBeVisible();
    await expect(planBadge).not.toBeVisible();

    // Plan toggle alone → only plan badge visible, no deep badge
    await planToggle.click();
    await expect(planBadge).toBeVisible({ timeout: 5000 });
    await expect(planBadge).toHaveText("Plan Mode");
    await expect(deepBadge).not.toBeVisible();
  });

  test("deep mode sends plan/start with deep_plan flag", async ({ page }) => {
    // Set up mock that intercepts plan/start and verifies deep_plan
    await setupMocks(page);

    let capturedBody: Record<string, unknown> | null = null;
    await page.route("**/api/chat/plan/start", (route: Route) => {
      const request = route.request();
      const body = request.postDataJSON();
      capturedBody = body;

      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockPlanSSE("1. Research conflicts\n2. Analyze data", "plan-001", true),
      });
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // Activate deep mode
    const deepToggle = page.locator('[data-testid="deep-toggle"]');
    await expect(deepToggle).toBeVisible({ timeout: 10000 });
    await deepToggle.click();

    // Send message → should call plan/start (not deep/start)
    const input = page.locator('[data-testid="chat-input"]');
    await input.fill("What are my conflicts?");
    await page.locator('[data-testid="send-button"]').click();

    // Wait for plan card to appear
    const planCard = page.locator('[data-testid="plan-card"]');
    await expect(planCard).toBeVisible({ timeout: 15000 });

    // Verify the request body contained deep_plan: true
    expect(capturedBody).not.toBeNull();
    expect(capturedBody!.deep_plan).toBe(true);
  });

  test("deep plan approval triggers deep reasoning SSE events", async ({ page }) => {
    await setupMocks(page);

    // Mock plan/start → returns deep plan
    await page.route("**/api/chat/plan/start", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockPlanSSE("1. Check calendar\n2. Find conflicts", "plan-deep-002", true),
      });
    });

    // Mock plan/approve → returns deep reasoning SSE
    await page.route("**/api/chat/plan/approve", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockDeepApproveSSE("You have 3 conflicts on Tuesday.", 23.5, 0.87),
      });
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    // Activate deep mode → send message → wait for plan card → approve
    const deepToggle = page.locator('[data-testid="deep-toggle"]');
    await expect(deepToggle).toBeVisible({ timeout: 10000 });
    await deepToggle.click();

    const input = page.locator('[data-testid="chat-input"]');
    await input.fill("What conflicts this week?");
    await page.locator('[data-testid="send-button"]').click();

    // Plan card should appear
    const planCard = page.locator('[data-testid="plan-card"]');
    await expect(planCard).toBeVisible({ timeout: 15000 });

    // Plan card should be styled for deep plan (violet border)
    await expect(planCard.locator("text=Deep Research Plan")).toBeVisible();

    // Click approve
    await page.locator('[data-testid="plan-approve"]').click();

    // Wait for assistant response with deep result
    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 15000 });

    // Should contain the response text and cost
    await expect(assistantMsg).toContainText("3 conflicts on Tuesday");
    await expect(assistantMsg).toContainText("RLM: 23.5s / $0.87");
  });

  test("deep plan error during approval shows error", async ({ page }) => {
    await setupMocks(page);

    await page.route("**/api/chat/plan/start", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: mockPlanSSE("Research plan...", "plan-err-003", true),
      });
    });

    await page.route("**/api/chat/plan/approve", (route: Route) => {
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
        body: buildSSE([
          { event: "deep_start", data: { deep_id: "dp-err", query: "test" } },
          { event: "error", data: { error: "RLM service unavailable" } },
          { event: "done", data: { text: "" } },
        ]),
      });
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const deepToggle = page.locator('[data-testid="deep-toggle"]');
    await expect(deepToggle).toBeVisible({ timeout: 10000 });
    await deepToggle.click();

    const input = page.locator('[data-testid="chat-input"]');
    await input.fill("Will fail");
    await page.locator('[data-testid="send-button"]').click();

    const planCard = page.locator('[data-testid="plan-card"]');
    await expect(planCard).toBeVisible({ timeout: 15000 });
    await page.locator('[data-testid="plan-approve"]').click();

    const assistantMsg = page.locator('[data-testid="message-assistant"]').last();
    await expect(assistantMsg).toBeVisible({ timeout: 15000 });
    await expect(assistantMsg).toContainText("RLM service unavailable");
  });

  test("Ctrl+Shift+D activates deep plan mode", async ({ page }) => {
    await setupMocks(page);
    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const deepBadge = page.locator('[data-testid="deep-mode-badge"]');
    const planBadge = page.locator('[data-testid="plan-mode-badge"]');
    const input = page.locator('[data-testid="chat-input"]');
    await expect(input).toBeVisible({ timeout: 10000 });
    await expect(deepBadge).not.toBeVisible();

    // Ctrl+Shift+D → activate deep (which also activates plan)
    await page.keyboard.press("Control+Shift+KeyD");
    await expect(deepBadge).toBeVisible({ timeout: 5000 });
    await expect(deepBadge).toContainText("Deep Plan");

    // Plan-only badge should NOT be visible when deep is active
    await expect(planBadge).not.toBeVisible();

    // Ctrl+Shift+D → both off
    await page.keyboard.press("Control+Shift+KeyD");
    await expect(deepBadge).not.toBeVisible();
    await expect(planBadge).not.toBeVisible();
  });
});
