/**
 * Chat polish in the real browser, every backend intercepted: copying a
 * message to the system clipboard, regenerating a reply, and paging back
 * through history with "Load earlier".
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const MANIFESTS = {
  default_agent: "main",
  count: 1,
  broken: [],
  agents: [{ id: "main", name: "Robothor", chattable: true, session_target: "isolated" }],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function sse(events: Array<{ event: string; data: unknown }>): string {
  return events.map((ev) => `event: ${ev.event}\ndata: ${JSON.stringify(ev.data)}`).join("\n\n") + "\n\n";
}

const NEWEST = {
  hasMore: true,
  messages: [
    { id: 3, role: "user", content: "what is two plus two", timestamp: "2026-10-31T09:03:00+00:00" },
    { id: 4, role: "assistant", content: "**Four**, as `2 + 2`.", timestamp: "2026-10-31T09:04:00+00:00" },
  ],
};

const OLDER = {
  hasMore: false,
  messages: [
    { id: 1, role: "user", content: "good morning", timestamp: "2026-10-30T08:00:00+00:00" },
    { id: 2, role: "assistant", content: "Morning!", timestamp: "2026-10-30T08:01:00+00:00" },
  ],
};

async function setupMocks(page: Page) {
  const sends: Array<Record<string, unknown>> = [];
  const historyUrls: string[] = [];
  await page.route("**/api/bridge/api/agent-manifests", (route) => json(route, MANIFESTS));
  await page.route("**/api/chat/history*", (route) => {
    const url = new URL(route.request().url());
    historyUrls.push(url.search);
    return json(route, url.searchParams.get("before") === "3" ? OLDER : NEWEST);
  });
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
      body: "event: ping\ndata: {}\n\n",
    }),
  );
  await page.route("**/api/chat/send", (route) => {
    sends.push(JSON.parse(route.request().postData() || "{}"));
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: sse([{ event: "done", data: { text: "Still four." } }]),
    });
  });
  return { sends, historyUrls };
}

test.describe("Chat polish", () => {
  test("copy puts the message on the clipboard, as markdown and as plain text", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await setupMocks(page);
    await page.goto("/", { waitUntil: "networkidle" });

    const reply = page.locator('[data-testid="message-assistant"]').last();
    await expect(reply).toContainText("Four");
    await reply.hover();
    await reply.getByRole("button", { name: "Copy message" }).click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("**Four**, as `2 + 2`.");

    await reply.getByRole("button", { name: "Copy as plain text" }).click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("Four, as 2 + 2.");
  });

  test("regenerate re-sends the question once, from the keyboard too", async ({ page }) => {
    const { sends } = await setupMocks(page);
    await page.goto("/", { waitUntil: "networkidle" });

    const reply = page.locator('[data-testid="message-assistant"]').last();
    await expect(reply).toContainText("Four");
    const regen = reply.getByRole("button", { name: "Regenerate response" });
    await regen.focus();
    await page.keyboard.press("Enter");

    await expect.poll(() => sends.length).toBe(1);
    expect(sends[0]).toEqual({ message: "what is two plus two", request_id: expect.any(String) });
    await expect(page.getByText("Still four.")).toBeVisible();
  });

  test("load earlier pages back by cursor and shows the stored times", async ({ page }) => {
    const { historyUrls } = await setupMocks(page);
    await page.goto("/", { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="message-assistant"]').last()).toContainText("Four");
    await expect(page.locator("[data-testid=message-list] time")).toHaveCount(2);

    await page.getByRole("button", { name: /load earlier/i }).click();
    await expect(page.getByText("good morning")).toBeVisible();

    expect(historyUrls.some((q) => q.includes("before=3"))).toBe(true);
    const texts = await page.locator("[data-testid=message-user], [data-testid=message-assistant]").allTextContents();
    expect(texts.findIndex((t) => t.includes("good morning"))).toBeLessThan(
      texts.findIndex((t) => t.includes("what is two plus two")),
    );
    await expect(page.locator("[data-testid=message-list] time")).toHaveCount(4);
    await expect(page.getByRole("button", { name: /load earlier/i })).toHaveCount(0);
  });
});
