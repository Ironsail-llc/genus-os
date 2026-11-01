/**
 * Dashboard performance budgets, measured in a real browser.
 *
 * Every backend is intercepted (the same mocks chat-agents.spec.ts uses), so
 * the numbers are the dashboard's own cost: bundle parse, hydration, first
 * render of the chat, and the client's handling of a streamed reply. They are
 * budgets, not benchmarks — generous enough for a shared CI runner, tight
 * enough that a change which doubles the cost fails here. Override them with
 * PERF_CHAT_PAINT_BUDGET_MS and PERF_FIRST_DELTA_BUDGET_MS when measuring.
 *
 * The connection count guards the live event bus: one tab holds exactly one
 * EventSource, opened once. A second mounted consumer of useEventStream, or a
 * hook that reconnects whenever its caller re-renders, multiplies the server's
 * SSE connections per operator without anything on screen changing.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const CHAT_PAINT_BUDGET_MS = Number(process.env.PERF_CHAT_PAINT_BUDGET_MS ?? 5000);
const FIRST_DELTA_BUDGET_MS = Number(process.env.PERF_FIRST_DELTA_BUDGET_MS ?? 1500);
const DELTA_TOKEN = "first-delta-marker";

const MANIFESTS = {
  default_agent: "main",
  count: 1,
  broken: [],
  agents: [{ id: "main", name: "Robothor", chattable: true, session_target: "isolated" }],
};

function sse(events: Array<{ event: string; data: unknown }>): string {
  return events.map((ev) => `event: ${ev.event}\ndata: ${JSON.stringify(ev.data)}`).join("\n\n") + "\n\n";
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function setupMocks(page: Page) {
  await page.route("**/api/bridge/api/agent-manifests", (route) => json(route, MANIFESTS));
  await page.route("**/api/chat/history*", (route) => json(route, { messages: [] }));
  await page.route("**/api/chat/plan/status*", (route) => json(route, { active: false }));
  await page.route("**/api/chat/deep/status*", (route) => json(route, { active: false }));
  await page.route("**/api/session", (route) => json(route, {}));
  await page.route("**/api/dashboard/welcome", (route) => json(route, { html: "<div>Welcome</div>", type: "html" }));
  await page.route("**/api/dashboard/generate", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/health", (route) => json(route, { status: "healthy", agents: {} }));
  await page.route("**/api/chat/send", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: sse([
        { event: "delta", data: { text: `${DELTA_TOKEN} ` } },
        { event: "delta", data: { text: "and the rest." } },
        { event: "done", data: { text: `${DELTA_TOKEN} and the rest.` } },
      ]),
    })
  );
}

/**
 * Instruments the page before any of its scripts run: counts EventSources
 * constructed and still open, and timestamps — on the page's own
 * clock, milliseconds since navigation start — when the chat input first
 * exists, when send is clicked, and when the first delta's text is on screen.
 */
async function instrument(page: Page) {
  await page.addInitScript((token: string) => {
    type Perf = {
      eventSources: { opened: number; live: number };
      chatInputAt: number | null;
      sendAt: number | null;
      firstDeltaAt: number | null;
    };
    const perf: Perf = { eventSources: { opened: 0, live: 0 }, chatInputAt: null, sendAt: null, firstDeltaAt: null };
    (window as unknown as { __perf: Perf }).__perf = perf;

    // A stand-in that connects once and stays open, as a healthy event bus
    // does. Playwright cannot hold a mocked stream open, and a real one that
    // ends would make the client's own error-and-retry path the thing being
    // counted. With a connection that never drops, every construction after
    // the first is a reconnect the dashboard chose to make.
    class CountingEventSource extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSED = 2;
      readonly url: string;
      readonly withCredentials = false;
      readyState = 0;
      onopen: ((ev: Event) => void) | null = null;
      onmessage: ((ev: MessageEvent) => void) | null = null;
      onerror: ((ev: Event) => void) | null = null;
      constructor(url: string | URL) {
        super();
        this.url = String(url);
        perf.eventSources.opened += 1;
        perf.eventSources.live += 1;
        setTimeout(() => {
          if (this.readyState !== 0) return;
          this.readyState = 1;
          const ev = new Event("open");
          this.onopen?.(ev);
          this.dispatchEvent(ev);
        }, 0);
      }
      close() {
        if (this.readyState !== 2) {
          this.readyState = 2;
          perf.eventSources.live -= 1;
        }
      }
    }
    window.EventSource = CountingEventSource as unknown as typeof EventSource;

    const observe = () => {
      const check = () => {
        if (perf.chatInputAt === null && document.querySelector('[data-testid="chat-input"]')) {
          perf.chatInputAt = performance.now();
        }
        if (perf.sendAt !== null && perf.firstDeltaAt === null) {
          const list = document.querySelector('[data-testid="message-list"]');
          if (list?.textContent?.includes(token)) perf.firstDeltaAt = performance.now();
        }
      };
      new MutationObserver(check).observe(document.documentElement, {
        childList: true,
        subtree: true,
        characterData: true,
      });
      check();
    };
    document.addEventListener(
      "click",
      (event) => {
        const target = event.target as Element | null;
        if (target?.closest('[data-testid="send-button"]')) perf.sendAt = performance.now();
      },
      true
    );
    if (document.documentElement) observe();
    else document.addEventListener("DOMContentLoaded", observe);
  }, DELTA_TOKEN);
}

type PerfReadout = {
  eventSources: { opened: number; live: number };
  chatInputAt: number | null;
  sendAt: number | null;
  firstDeltaAt: number | null;
};

const readout = (page: Page) => page.evaluate(() => (window as unknown as { __perf: PerfReadout }).__perf);

test.describe("Dashboard performance budgets", () => {
  test("chat paints, streams its first delta and holds one event stream within budget", async ({ page }, testInfo) => {
    const vitals: string[] = [];
    await page.route("**/api/vitals", (route) => {
      try {
        vitals.push(JSON.parse(route.request().postData() || "{}").name);
      } catch {
        /* a malformed beacon fails the assertion below */
      }
      return route.fulfill({ status: 204, body: "" });
    });
    await setupMocks(page);
    await instrument(page);

    await page.goto("/", { waitUntil: "networkidle" });
    const input = page.locator('[data-testid="chat-input"]');
    await expect(input).toBeVisible({ timeout: 10000 });

    const painted = await readout(page);
    const fcp = await page.evaluate(
      () => performance.getEntriesByName("first-contentful-paint")[0]?.startTime ?? null
    );

    await input.fill("hello");
    await page.locator('[data-testid="send-button"]').click();
    await expect(page.locator('[data-testid="message-list"]')).toContainText(DELTA_TOKEN);
    await expect.poll(async () => (await readout(page)).firstDeltaAt).not.toBeNull();
    // Let the turn's re-renders settle before counting connections.
    await page.waitForTimeout(1000);

    const done = await readout(page);
    // The input is in the server-rendered HTML, so it exists before anything
    // is painted: the chat is on screen at whichever comes later.
    const chatPaintMs = Math.max(painted.chatInputAt!, fcp ?? 0);
    const firstDeltaMs = done.firstDeltaAt! - done.sendAt!;
    const measured = {
      fcp_ms: fcp === null ? null : Math.round(fcp),
      chat_paint_ms: Math.round(chatPaintMs),
      first_delta_ms: Math.round(firstDeltaMs),
      event_sources_opened: done.eventSources.opened,
      event_sources_live: done.eventSources.live,
    };
    console.log(`perf ${JSON.stringify(measured)}`);
    testInfo.annotations.push({ type: "perf", description: JSON.stringify(measured) });

    expect(chatPaintMs, `chat input visible ${Math.round(chatPaintMs)} ms after navigation start`).toBeLessThan(
      CHAT_PAINT_BUDGET_MS
    );
    expect(firstDeltaMs, `first delta rendered ${Math.round(firstDeltaMs)} ms after send`).toBeLessThan(
      FIRST_DELTA_BUDGET_MS
    );
    expect(done.eventSources.opened, "EventSources constructed in this tab").toBe(1);
    expect(done.eventSources.live, "EventSources open in this tab").toBe(1);

    // The reporter is mounted: leaving the page flushes the vitals it holds.
    await page.evaluate(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => vitals.length, { timeout: 10000 }).toBeGreaterThan(0);
    expect(vitals.every((name) => ["CLS", "FCP", "FID", "INP", "LCP", "TTFB"].includes(name))).toBe(true);
  });
});
