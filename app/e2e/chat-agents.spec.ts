/**
 * Talking to another agent, and answering a tool-permission escalation, in the
 * real browser.
 *
 * Every backend is intercepted, so what this proves is the wiring the unit
 * tests cannot: that the panel a person actually clicks puts the chosen agent
 * in the body it sends, and that the escalation button posts to the ESCALATION
 * route rather than the question one. Before the `kind` branch existed, that
 * second POST went to `/api/approvals/question/{id}`, where the id meant
 * nothing — the operator saw "Answered" and the agent stayed blocked until its
 * timeout denied the tool for it.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const BASE_URL = "/";

const MANIFESTS = {
  default_agent: "main",
  count: 2,
  broken: [],
  agents: [
    { id: "main", name: "Robothor", chattable: true, session_target: "isolated" },
    { id: "scheduler", name: "Scheduler", chattable: true, session_target: "persistent" },
  ],
};

const ESCALATION = {
  kind: "escalation",
  id: "esc-e2e",
  run_id: "run-e2e",
  agent_id: "scheduler",
  tool: "exec",
  question: "Agent scheduler is requesting approval.\nTool: exec",
  options: ["Approve", "Approve All", "Deny"],
  timeout_seconds: 300,
};

function sse(events: Array<{ event: string; data: unknown }>): string {
  return (
    events.map((ev) => `event: ${ev.event}\ndata: ${JSON.stringify(ev.data)}`).join("\n\n") + "\n\n"
  );
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function setupMocks(page: Page, sendBody: string) {
  await page.route("**/api/bridge/api/agent-manifests", (route) => json(route, MANIFESTS));
  await page.route("**/api/chat/history*", (route) => json(route, { messages: [] }));
  await page.route("**/api/chat/plan/status*", (route) => json(route, { active: false }));
  await page.route("**/api/chat/deep/status*", (route) => json(route, { active: false }));
  await page.route("**/api/session", (route) => json(route, {}));
  await page.route("**/api/dashboard/welcome", (route) =>
    json(route, { html: "<div>Welcome</div>", type: "html" })
  );
  await page.route("**/api/dashboard/generate", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/health", (route) => json(route, { status: "healthy", agents: {} }));
  await page.route("**/api/events/stream*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: "event: ping\ndata: {}\n\n",
    })
  );
  await page.route("**/api/chat/send", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: sendBody,
    })
  );
}

test.describe("Chat — another agent, and an escalation answered in place", () => {
  test("switching agent puts that agent in the send body", async ({ page }) => {
    await setupMocks(page, sse([{ event: "done", data: { text: "On it." } }]));

    const bodies: Array<Record<string, unknown>> = [];
    page.on("request", (request) => {
      if (request.url().endsWith("/api/chat/send")) {
        bodies.push(JSON.parse(request.postData() || "{}"));
      }
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const input = page.locator('[data-testid="chat-input"]');
    await expect(input).toBeVisible({ timeout: 10000 });

    // The default agent first: the body must carry NO agent at all, because
    // that omission is what keeps the operator's shared main session shared.
    await input.fill("morning");
    await page.locator('[data-testid="send-button"]').click();
    await expect.poll(() => bodies.length).toBe(1);
    expect(bodies[0]).toEqual({ message: "morning", request_id: expect.any(String) });

    const switcher = page.locator('[data-testid="agent-switcher"]');
    await expect(switcher).toBeVisible();
    // The default option's value is the empty string — "send no key" — and its
    // label is the main agent's name, so the header cannot name one agent while
    // the message lands in another's session.
    await expect(switcher).toHaveValue("");
    await switcher.selectOption("scheduler");

    await input.fill("what's on today?");
    await page.locator('[data-testid="send-button"]').click();
    await expect.poll(() => bodies.length).toBe(2);
    expect(bodies[1]).toEqual({ message: "what's on today?", agent: "scheduler", request_id: expect.any(String) });

    // And back: the operator can always return to their own conversation.
    await switcher.selectOption("");
    await input.fill("back to you");
    await page.locator('[data-testid="send-button"]').click();
    await expect.poll(() => bodies.length).toBe(3);
    expect(bodies[2]).toEqual({ message: "back to you", request_id: expect.any(String) });
  });

  test("an escalation on the stream is answered at the escalation route", async ({ page }) => {
    await setupMocks(
      page,
      sse([
        { event: "approval_required", data: ESCALATION },
        { event: "done", data: { text: "Waiting on you." } },
      ])
    );

    let answered: { url: string; body: unknown } | null = null;
    await page.route("**/api/bridge/api/approvals/**", (route) => {
      answered = { url: route.request().url(), body: JSON.parse(route.request().postData() || "{}") };
      return json(route, { settled: true, kind: "escalation", id: ESCALATION.id });
    });

    await page.goto(BASE_URL, { waitUntil: "networkidle" });

    const input = page.locator('[data-testid="chat-input"]');
    await expect(input).toBeVisible({ timeout: 10000 });
    await input.fill("tidy the logs");
    await page.locator('[data-testid="send-button"]').click();

    const card = page.locator('[data-testid="escalation-card"]');
    await expect(card).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="escalation-tool"]')).toHaveText("exec");
    // A real number of seconds, counted down from `timeout_seconds`. `"s."`
    // alone matched the sentence's full stop and would have passed on a
    // countdown that never started.
    await expect(page.locator('[data-testid="escalation-countdown"]')).toHaveText(
      /denies this itself in \d+s/
    );

    await page.locator('[data-testid="escalation-allow-once"]').click();

    await expect.poll(() => answered !== null).toBe(true);
    expect(answered!.url).toContain(`/api/bridge/api/approvals/escalation/${ESCALATION.id}`);
    expect(answered!.body).toEqual({ approved: true });
    await expect(page.locator('[data-testid="ask-status"]')).toContainText("Allow once");
  });
});
