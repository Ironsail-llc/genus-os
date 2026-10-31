/**
 * The live event stream: one EventSource per tab, and it survives re-renders.
 *
 * The app's EventSource is swapped for a counting stand-in before any page
 * script runs, so the spec measures what the app constructs, not how the
 * network mock behaves. Navigating between views re-renders the shell; that
 * used to close and reopen the stream (and replay its recent events) on every
 * render.
 *
 * Overlap: the dashboard perf spec makes the same one-connection assertion.
 * Whichever lands second keeps one of them.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

declare global {
  interface Window {
    __sse: { constructed: string[]; open: number };
  }
}

async function countEventSources(page: Page) {
  await page.addInitScript(() => {
    const stats = { constructed: [] as string[], open: 0 };
    window.__sse = stats;
    class CountingEventSource extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readonly url: string;
      readonly withCredentials = false;
      readyState = 0;
      onopen: ((e: Event) => void) | null = null;
      onmessage: ((e: MessageEvent) => void) | null = null;
      onerror: ((e: Event) => void) | null = null;
      constructor(url: string | URL) {
        super();
        this.url = String(url);
        stats.constructed.push(this.url);
        stats.open++;
        setTimeout(() => {
          if (this.readyState === 2) return;
          this.readyState = 1;
          const e = new Event("open");
          this.onopen?.(e);
          this.dispatchEvent(e);
        }, 0);
      }
      close() {
        if (this.readyState !== 2) stats.open--;
        this.readyState = 2;
      }
    }
    (window as unknown as { EventSource: unknown }).EventSource = CountingEventSource;
  });
}

async function setupMocks(page: Page) {
  // Anything not mocked below answers empty rather than reaching the server.
  await page.route("**/api/**", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "{}" }),
  );
  await page.route("**/api/chat/history", (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ messages: [] }) }),
  );
  await page.route("**/api/actions/execute", (route: Route) => {
    const body = route.request().postDataJSON() as { tool?: string } | null;
    const payload =
      body?.tool === "agent_status"
        ? { agents: [{ name: "worker", schedule: "hourly", status: "healthy" }] }
        : { tasks: [{ id: "t1", title: "A task", status: "TODO" }] };
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: payload }) });
  });
}

test.describe("Live event stream", () => {
  test("opens exactly one EventSource per tab and keeps it across views", async ({ page }) => {
    await countEventSources(page);
    await setupMocks(page);
    await page.goto("/", { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="app-shell"]')).toBeVisible({ timeout: 10000 });

    for (const view of ["dashboard", "tasks", "agents", "chat"]) {
      await page.locator(`[data-testid="nav-${view}"]`).first().click();
      await page.waitForTimeout(300);
    }

    const stats = await page.evaluate(() => window.__sse);
    expect(stats.constructed).toHaveLength(1);
    expect(stats.constructed[0]).toContain("/api/events/stream");
    expect(stats.open).toBe(1);
  });
});
