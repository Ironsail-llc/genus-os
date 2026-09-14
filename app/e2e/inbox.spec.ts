/**
 * The Inbox, end to end, against a bridge that lives entirely in `page.route`.
 *
 * This spec must never reach a real bridge: every act it performs settles a
 * waiting approval or answers an agent's question, and doing that on the box
 * would resume a production run.
 *
 * The mock keeps state, so the assertions are about the round trip rather than
 * a canned reply — a card the operator answers has to be gone from the NEXT
 * listing too, not merely hidden by local state.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const INBOX_URL = "/?v=inbox";

const QUESTION_ID = "11111111-1111-4111-8111-111111111111";
const APPROVAL_ID = "22222222-2222-4222-8222-222222222222";

interface Pending {
  kind: "workflow" | "question";
  id: string;
  run_id: string;
  agent_id: string;
  question: string;
  detail: string;
  options: string[];
  expires_at: string;
  created_at: string;
}

const SEED: Pending[] = [
  {
    kind: "question",
    id: QUESTION_ID,
    run_id: "abcdef12-3456-4789-8abc-def012345678",
    agent_id: "invoice-chaser",
    question: "Which vendor should I chase first?",
    detail: "",
    options: ["Alice", "Bob"],
    expires_at: "2099-01-01T13:00:00Z",
    created_at: "2026-06-15T11:30:00Z",
  },
  {
    kind: "workflow",
    id: APPROVAL_ID,
    run_id: "99887766-5544-4332-8110-aabbccddeeff",
    agent_id: "",
    question: "Send the quote to agent@example.com?",
    detail: "The draft sits on the run and has not been sent anywhere yet.",
    options: [],
    expires_at: "2099-01-01T14:00:00Z",
    created_at: "2026-06-15T11:45:00Z",
  },
];

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

interface Recorded {
  answers: Array<{ kind: string; id: string; body: Record<string, unknown> }>;
}

async function setupMocks(page: Page): Promise<Recorded> {
  const store = new Map<string, Pending>(SEED.map((row) => [row.id, { ...row }]));
  const recorded: Recorded = { answers: [] };

  // An owner session: the listing and every answer are operator-gated.
  await page.route("**/api/auth/session", (route) =>
    json(route, {
      user: { name: "Operator", email: "operator@example.com" },
      role: "owner",
      expires: "2099-01-01T00:00:00.000Z",
    })
  );

  // The shell's own background traffic, kept off the box's real services.
  await page.route("**/api/chat/history", (route) => json(route, { messages: [] }));
  await page.route("**/api/chat/plan/status", (route) => json(route, { active: false, plan: null }));
  await page.route("**/api/chat/deep/status", (route) => json(route, { active: false, deep: null }));
  await page.route("**/api/session", (route) => json(route, {}));
  await page.route("**/api/health", (route) => json(route, { status: "ok", services: [] }));
  await page.route("**/api/actions/execute", (route) =>
    json(route, { data: { agents: [] }, ok: true })
  );
  await page.route("**/api/events/stream*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: "event: ping\ndata: {}\n\n",
    })
  );

  await page.route("**/api/bridge/api/approvals", (route) => {
    const pending = [...store.values()];
    return json(route, { count: pending.length, pending });
  });

  await page.route("**/api/bridge/api/approvals/*/*", (route) => {
    const segments = new URL(route.request().url()).pathname.split("/");
    const id = segments[segments.length - 1];
    const kind = segments[segments.length - 2];
    const body = route.request().postDataJSON() as Record<string, unknown>;
    recorded.answers.push({ kind, id, body });

    if (!store.has(id)) return json(route, { settled: false, message: "Already answered." });
    store.delete(id);

    if (kind === "question") return json(route, { settled: true, kind, id });
    return json(route, {
      settled: true,
      kind,
      id,
      decision: body.approved ? "approved" : "rejected",
    });
  });

  return recorded;
}

test.describe("Inbox › answering in place", () => {
  test("answers a question and approves an approval, and the badge empties", async ({ page }) => {
    const recorded = await setupMocks(page);
    await page.goto(INBOX_URL, { waitUntil: "networkidle" });

    // Both kinds, each with its own pill and the agent that raised it.
    await expect(page.locator(`[data-testid="inbox-card-${QUESTION_ID}"]`)).toBeVisible({
      timeout: 15000,
    });
    await expect(page.locator(`[data-testid="inbox-kind-${QUESTION_ID}"]`)).toHaveText("Question");
    await expect(page.locator(`[data-testid="inbox-who-${QUESTION_ID}"]`)).toHaveText(
      "invoice-chaser"
    );
    await expect(page.locator(`[data-testid="inbox-kind-${APPROVAL_ID}"]`)).toHaveText("Approval");
    // A workflow row carries no agent, and the card says so rather than blank.
    await expect(page.locator(`[data-testid="inbox-who-${APPROVAL_ID}"]`)).toHaveText("workflow");
    // The short run id sits beside the Runs link; there is no per-run deep link yet.
    await expect(page.locator(`[data-testid="inbox-run-id-${QUESTION_ID}"]`)).toHaveText(
      "abcdef12"
    );

    await expect(page.locator('[data-testid="badge-inbox"]')).toHaveText("2");

    // The question: the option the operator clicked is the answer that is sent.
    await page.locator(`[data-testid="inbox-option-${QUESTION_ID}-1"]`).click();
    await expect(page.locator(`[data-testid="inbox-card-${QUESTION_ID}"]`)).toHaveCount(0);
    await expect(page.locator('[data-testid="badge-inbox"]')).toHaveText("1");

    // The approval: a verdict plus the note beside the button.
    await page.locator(`[data-testid="inbox-note-${APPROVAL_ID}"]`).fill("Figures check out.");
    await page.locator(`[data-testid="inbox-approve-${APPROVAL_ID}"]`).click();
    await expect(page.locator(`[data-testid="inbox-card-${APPROVAL_ID}"]`)).toHaveCount(0);

    expect(recorded.answers).toEqual([
      { kind: "question", id: QUESTION_ID, body: { answer: "Bob" } },
      { kind: "workflow", id: APPROVAL_ID, body: { approved: true, note: "Figures check out." } },
    ]);

    // Nothing waiting: no badge anywhere, and the empty state says where
    // review tasks still live.
    await expect(page.locator('[data-testid="badge-inbox"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="inbox-empty"]')).toContainText(
      "Nothing is waiting on you."
    );
    await expect(page.locator('[data-testid="inbox-empty"]')).toContainText("Tasks");
  });

  test("stays usable at phone width", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(INBOX_URL, { waitUntil: "networkidle" });

    const card = page.locator(`[data-testid="inbox-card-${APPROVAL_ID}"]`);
    await expect(card).toBeVisible({ timeout: 15000 });

    // Nothing overflows the viewport, and the answer field is full width.
    const box = await card.boundingBox();
    expect(box).not.toBeNull();
    expect((box?.width ?? 0) + (box?.x ?? 0)).toBeLessThanOrEqual(391);

    await expect(page.locator(`[data-testid="inbox-approve-${APPROVAL_ID}"]`)).toBeVisible();
    await expect(page.locator(`[data-testid="inbox-note-${APPROVAL_ID}"]`)).toBeVisible();

    // The phone tab bar carries the same count from the same poll.
    await expect(page.locator('[data-testid="badge-inbox"]')).toHaveText("2");
  });
});
