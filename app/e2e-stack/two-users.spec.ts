/**
 * The dry run's two-user flow, against the real stack. Alice makes a daily
 * sales report by asking the main agent and shares it with Bob; Bob finds it,
 * opens it from a link and works in it; both of them post at once; Bob cannot
 * change what Alice defined but can duplicate it; Alice restores a revision.
 *
 * One test per step of the dry-run script, in order (serial). A step that
 * needs work still being built elsewhere says so with `expectedToFailUntil`
 * and runs anyway, so the day that work merges it shows up as an unexpected
 * pass and the mark comes off.
 */
import { expect, test, type Page } from "@playwright/test";
import { api, expectedToFailUntil, outboxCount, people, requireStack, roomLink, say, signedIn } from "./stack";

requireStack();
test.describe.configure({ mode: "serial" });

const ROOM_ID = /room=([0-9a-f-]{36})/;
let roomId = "";
let alicePage: Page;
let bobPage: Page;

function room(page: Page) {
  return page.getByTestId("room-view");
}

test.beforeAll(async ({ browser }) => {
  alicePage = (await signedIn(browser, "alice")).page;
  bobPage = (await signedIn(browser, "bob")).page;
});

test("1. Alice asks the main agent for a daily sales report; it restates, then creates it (A1)", async () => {
  await alicePage.goto("/?v=chat");
  // Earlier runs' turns load first; count only once the history is in.
  await settled(alicePage.getByTestId("message-row"));
  const restated = alicePage.getByText(/I will create a report workspace “Daily sales”/);
  const restatedBefore = await restated.count();
  await say(alicePage, "Please create a daily sales report", { main: true });
  await expect(restated).toHaveCount(restatedBefore + 1, { timeout: 30000 });
  // The main conversation keeps earlier runs' answers: wait for a NEW link.
  const answers = alicePage.getByText(/Open it: \/\?v=chat&room=/);
  const before = await answers.count();
  await say(alicePage, "yes, create it. share with bob@example.com", { main: true });
  await expect(answers).toHaveCount(before + 1, { timeout: 30000 });
  roomId = ROOM_ID.exec((await answers.last().textContent()) ?? "")?.[1] ?? "";
  expect(roomId).not.toBe("");
  const made = await (await api("alice")).get(`/api/rooms/${roomId}`);
  expect(made.body).toMatchObject({ kind: "report", access: "assigned", settings: { schedule_text: "0 7 * * *", tz: "UTC" } });
  expect((made.body.members as Array<{ displayName: string }>).map((m) => m.displayName).sort()).toEqual(["Alice", "Bob"]);
});

test("1b. Alice previews it before it runs on schedule; only she sees the preview (A2)", async () => {
  const done = alicePage.getByText(/Done \(room_run\)/);
  const before = await done.count();
  await say(alicePage, `preview ${roomId}`, { main: true });
  await expect(done).toHaveCount(before + 1, { timeout: 30000 });
  // The preview is built, for Alice only.
  await expect
    .poll(async () => (await editions("alice")).filter((e) => e.preview).length, { timeout: 60000 })
    .toBeGreaterThan(0);
  const bobsEditions = await (await api("bob")).get(`/api/rooms/${roomId}/editions`);
  expect((bobsEditions.body.editions as Array<{ preview: boolean }>).some((e) => e.preview)).toBe(false);
});

test("2. the schedule and sharing show in the workspace (A3, G1, G7)", async () => {
  await alicePage.goto(roomLink(roomId));
  await expect(room(alicePage).getByTestId("room-header")).toContainText("Daily sales");
  await alicePage.getByTestId("room-settings-open").click();
  const drawer = alicePage.getByTestId("room-settings");
  await expect(drawer.getByRole("textbox", { name: /schedule/i })).toHaveValue("0 7 * * *");
  await expect(drawer.getByTestId("settings-next-run")).not.toBeEmpty();
  await alicePage.keyboard.press("Escape");
  await alicePage.getByTestId("room-share-open").click();
  await expect(alicePage.getByTestId("share-dialog")).toContainText("Bob");
  await expect(alicePage.getByTestId("share-runs-as")).toContainText("Alice");
  await alicePage.keyboard.press("Escape");
});

type EditionRow = {
  id: string;
  seq: number;
  status: string;
  preview: boolean;
  error?: string | null;
  suggestions?: Array<{ key: string }>;
  provenance?: Record<string, unknown>;
};

async function editions(who: "alice" | "bob", id = roomId): Promise<EditionRow[]> {
  return ((await (await api(who)).get(`/api/rooms/${id}/editions`)).body.editions ?? []) as EditionRow[];
}

test("3. run now builds and publishes an edition from the real run, with its provenance (B6, B2, B3)", async () => {
  const run = await (await api("bob")).post(`/api/rooms/${roomId}/run`, {});
  expect([200, 202]).toContain(run.status);
  await expect
    .poll(async () => (await editions("bob")).filter((e) => !e.preview && e.status === "published").length, {
      timeout: 60000,
    })
    .toBeGreaterThan(0);
  const [published] = (await editions("bob")).filter((e) => !e.preview && e.status === "published");
  const provenance = published.provenance ?? {};
  // The run id was recorded at the run's first status event, by the real runner.
  expect(typeof provenance.run_id === "string" && provenance.run_id).toBeTruthy();
  expect((provenance.tool_calls as Array<{ tool: string }>).map((c) => c.tool)).toContain("sample_sales_figures");
  expect(typeof provenance.cost_usd).toBe("number");
});

test("3b. the provenance drawer shows how the edition was made and what it cost (B3, B5)", async () => {
  await bobPage.goto(roomLink(roomId));
  await expect(bobPage.getByTestId("room-pinned-edition")).toBeVisible({ timeout: 30000 });
  await bobPage.getByTestId("room-more").click();
  await bobPage.getByTestId("room-menu-provenance").click();
  const drawer = bobPage.getByTestId("provenance-drawer");
  await expect(drawer.getByTestId("provenance-ran-as")).toContainText("Alice");
  await expect(drawer.getByTestId("provenance-cost")).toContainText("$");
  await expect(drawer).toContainText("sample_sales_figures");
  await bobPage.keyboard.press("Escape");
});

test("4. Bob sees the workspace in his rail with unread activity (C1, C6)", async () => {
  // Alice's turn in the workspace is activity Bob has not read (he is elsewhere:
  // an open workspace marks what arrives as read).
  await bobPage.goto("/?v=chat");
  await alicePage.goto(roomLink(roomId));
  await say(alicePage, "Morning, Bob");
  await expect(room(alicePage).getByText("Answering Alice: Morning, Bob")).toBeVisible({ timeout: 30000 });
  await bobPage.goto("/?v=chat");
  const rail = bobPage.getByTestId("room-rail");
  await expect(rail.getByText("Daily sales").first()).toBeVisible({ timeout: 30000 });
  const mine = (await (await api("bob")).get("/api/rooms")).body.rooms as Array<{ id: string; unread: number }>;
  expect(mine.find((r) => r.id === roomId)?.unread).toBeGreaterThan(0);
  const alices = (await (await api("alice")).get("/api/rooms")).body.rooms as Array<{ id: string; unread: number }>;
  expect(alices.find((r) => r.id === roomId)?.unread ?? 0).toBe(0);
});

test("4b. a new edition leaves a note in Bob's main conversation (H3)", async () => {
  await bobPage.goto("/?v=chat");
  // The note is the edition's headline and summary, and a link to the workspace;
  // never the edition itself.
  const note = bobPage.getByTestId("message-row").filter({ hasText: /Sales \d{4}-\d{2}-\d{2}: Revenue/ }).last();
  await expect(note).toBeVisible({ timeout: 30000 });
  await expect(note.getByRole("link", { name: "Daily sales" })).toHaveAttribute("href", new RegExp(roomId));
});

test("5. Bob opens it from a link and reads it; his unread clears (C2, C6)", async () => {
  await bobPage.goto(roomLink(roomId));
  await expect(room(bobPage).getByTestId("room-header")).toContainText("Daily sales");
  await expect(room(bobPage).getByText("Answering Alice: Morning, Bob")).toBeVisible({ timeout: 30000 });
  await expect
    .poll(async () => {
      const rows = (await (await api("bob")).get("/api/rooms")).body.rooms as Array<{ id: string; unread: number }>;
      return rows.find((r) => r.id === roomId)?.unread;
    }, { timeout: 15000 })
    .toBe(0);
});

test("6. Bob's message is attributed and the agent answers him (D1, D2)", async () => {
  await say(bobPage, "How did we do yesterday?");
  await expect(room(bobPage).getByText("Answering Bob: How did we do yesterday?")).toBeVisible({ timeout: 30000 });
  await alicePage.goto(roomLink(roomId));
  const row = room(alicePage).getByTestId("message-row").filter({ hasText: "How did we do yesterday?" });
  await expect(row.getByTestId("message-author")).toHaveText("Bob");
});

test("7. both post at once: the second is told who is being answered, and nothing is lost (D3)", async () => {
  await alicePage.goto(roomLink(roomId));
  await bobPage.goto(roomLink(roomId));
  await say(alicePage, "Summarise the week for me (take your time)");
  await expect(room(alicePage).getByTestId("message-row").filter({ hasText: "(take your time)" })).toBeVisible();
  await bobPage.waitForTimeout(1000);
  await say(bobPage, "And the returns?");
  await expect(room(bobPage).getByText(/Queued — answering Alice/)).toBeVisible({ timeout: 10000 });
  await expect(room(bobPage).getByText("Answering Bob: And the returns?")).toBeVisible({ timeout: 45000 });
  await bobPage.reload();
  await expect(room(bobPage).getByText(/Answering Alice: Summarise the week for me/)).toBeVisible({ timeout: 30000 });
  await expect(room(bobPage).getByText("Answering Bob: And the returns?")).toBeVisible();
});

test("8. Bob picks a past period and its edition is built (C3)", async () => {
  await bobPage.goto(roomLink(roomId));
  await bobPage.getByTestId("period-trigger").click();
  await bobPage.getByTestId("period-grain-day").click();
  await bobPage.getByTestId("period-anchor").fill(daysAgo(3));
  await bobPage.getByTestId("period-apply").click();
  await expect(bobPage.getByTestId("room-pinned-edition").getByTestId("edition-title")).toBeVisible({ timeout: 60000 });
});

let taskRoomId = "";

test("9. Bob accepts the edition's suggested task and opens it as its own workspace (E1, E2, E6)", async () => {
  const tasks = (await (await api("bob")).get(`/api/rooms/${roomId}/tasks`)).body.tasks as Array<{
    taskId: string;
    origin: string;
    title: string;
  }>;
  const suggested = tasks.find((t) => t.origin === "suggestion");
  expect(suggested?.title).toBe("Call the lapsed buyers");
  await bobPage.goto(roomLink(roomId));
  await bobPage.getByTestId(`room-task-accept-${suggested!.taskId}`).click();
  await expect(bobPage.getByTestId(`room-task-assignee-${suggested!.taskId}`)).toHaveText("Bob", { timeout: 15000 });
  await bobPage.getByTestId(`open-as-workspace-${suggested!.taskId}`).click();
  await expect(bobPage).not.toHaveURL(new RegExp(roomId), { timeout: 15000 });
  taskRoomId = ROOM_ID.exec(bobPage.url())?.[1] ?? "";
  expect(taskRoomId).not.toBe(roomId);
  const taskRoom = await (await api("bob")).get(`/api/rooms/${taskRoomId}`);
  expect(taskRoom.body).toMatchObject({ kind: "task", access: "assigned" });
});

async function proposeEmail(page: Page, id: string) {
  await page.goto(roomLink(id));
  const cards = page.getByTestId("room-approvals").locator('[data-testid^="room-email-"][aria-label]');
  const before = await cards.count();
  await say(page, "Please email the lapsed buyers a check-in");
  await expect(cards).toHaveCount(before + 1, { timeout: 30000 });
  return cards.last();
}

test("10. in the task workspace Bob sees the exact email, approves it, and it is sent once with a receipt (E3)", async () => {
  const sentBefore = outboxCount();
  const card = await proposeEmail(bobPage, taskRoomId);
  await expect(card.getByTestId("room-email-from")).toHaveText("assistant@example.com");
  await expect(card.getByTestId("room-email-reply-to")).toHaveText("bob@example.com");
  await expect(card.getByTestId("room-email-to")).toHaveText("buyer.one@example.org, buyer.two@example.org");
  await expect(card.getByTestId("room-email-subject")).toHaveText("Checking in");
  await expect(card.getByTestId("room-email-body")).toContainText("Is there anything we can help with?");

  // Alice can neither see Bob's action nor reach his task workspace.
  const alice = await api("alice");
  expect(JSON.stringify((await alice.get("/api/approvals")).body)).not.toContain(taskRoomId);
  expect((await alice.get(`/api/rooms/${taskRoomId}`)).status).toBe(404);

  await card.getByRole("button", { name: /approve and send/i }).click();
  const receipt = room(bobPage).getByTestId("room-email-receipt").last();
  await expect(receipt).toContainText("Sent", { timeout: 30000 });
  await expect(receipt).toContainText("buyer.one@example.org, buyer.two@example.org");
  expect(outboxCount()).toBe(sentBefore + 1);
  await bobPage.waitForTimeout(3000);
  expect(outboxCount()).toBe(sentBefore + 1); // exactly once
});

test("10a. the task is done once its email is sent (E3)", async () => {
  expectedToFailUntil(
    "a product decision: story E3 says the task completes, but nothing completes a task when its email is sent (a task may need more than one email)",
  );
  await expect
    .poll(async () => {
      const tasks = (await (await api("bob")).get(`/api/rooms/${roomId}/tasks`)).body.tasks as Array<{ status: string; origin: string }>;
      return tasks.find((t) => t.origin === "suggestion")?.status;
    }, { timeout: 15000 })
    .toBe("DONE");
});

test("10b. Bob rejects a second email: nothing is sent, and the receipt says so (E3)", async () => {
  const sentBefore = outboxCount();
  const card = await proposeEmail(bobPage, taskRoomId);
  await card.getByRole("button", { name: /reject/i }).click();
  await expect(room(bobPage).getByTestId("room-email-receipt").last()).toContainText(/Not sent: rejected/, {
    timeout: 30000,
  });
  expect(outboxCount()).toBe(sentBefore);
});

test("10c. the same request in the report workspace proposes nothing (E4)", async () => {
  const before = (await (await api("bob")).get("/api/approvals")).body;
  await bobPage.goto(roomLink(roomId));
  await say(bobPage, "Please email the lapsed buyers a check-in");
  await expect(room(bobPage).getByText(/room_propose_email was refused/).last()).toBeVisible({ timeout: 30000 });
  const after = (await (await api("bob")).get("/api/approvals")).body;
  expect(after.count).toBe(before.count);
});

test("11. a reload keeps the charts (C4, D4)", async () => {
  await bobPage.goto(roomLink(roomId));
  await bobPage.reload();
  await expect(bobPage.getByTestId("room-pinned-edition")).toBeVisible({ timeout: 30000 });
});

test("12. Bob cannot change the definition; duplicating gives him his own copy (G3, A8)", async () => {
  await bobPage.goto(roomLink(roomId));
  await bobPage.getByTestId("room-settings-open").click();
  const drawer = bobPage.getByTestId("room-settings");
  await expect(drawer.getByTestId("settings-readonly")).toBeVisible();
  await expect(drawer.getByTestId("settings-save")).toHaveCount(0);
  const refused = await (await api("bob")).patch(`/api/rooms/${roomId}`, { expectedRev: 1, changes: { title: "Mine" } });
  expect(refused.status).toBe(403);
  await drawer.getByTestId("settings-duplicate").click();
  await expect(bobPage).not.toHaveURL(new RegExp(roomId), { timeout: 30000 });
  const copyId = ROOM_ID.exec(bobPage.url())?.[1] ?? "";
  const copy = await (await api("bob")).get(`/api/rooms/${copyId}`);
  expect(copy.body).toMatchObject({ access: "assigned", paused: true, runsAs: { displayName: "Bob" } });
  const original = await (await api("alice")).get(`/api/rooms/${roomId}`);
  expect(original.body).toMatchObject({ title: "Daily sales", runsAs: { displayName: "Alice" } });
});

test("13. Alice changes the title, then restores revision 1 as a new revision (A3, A7)", async () => {
  await alicePage.goto(roomLink(roomId));
  await alicePage.getByTestId("room-settings-open").click();
  const drawer = alicePage.getByTestId("room-settings");
  await drawer.getByRole("textbox", { name: /title/i }).fill("Daily sales (EU)");
  await drawer.getByTestId("settings-save").click();
  await expect(drawer.getByTestId("settings-saved")).toBeVisible({ timeout: 15000 });
  await drawer.getByTestId("settings-tab-history").click();
  await drawer.getByTestId("revision-restore-1").click();
  await expect
    .poll(async () => (await (await api("alice")).get(`/api/rooms/${roomId}`)).body, { timeout: 15000 })
    .toMatchObject({ title: "Daily sales", rev: 3 });
});

/** Wait until a list has stopped growing (history loads after the page). */
async function settled(rows: ReturnType<Page["getByTestId"]>) {
  let last = -1;
  await expect
    .poll(
      async () => {
        const now = await rows.count();
        const same = now === last;
        last = now;
        return same;
      },
      { timeout: 20000, intervals: [1500] },
    )
    .toBe(true);
}

function daysAgo(n: number): string {
  return new Date(Date.now() - n * 86_400_000).toISOString().slice(0, 10);
}

test("14. nothing is capped: many on-demand periods in a row all start (C3)", async () => {
  const bob = await api("bob");
  const statuses: number[] = [];
  for (const n of [4, 5, 6, 7, 8, 9]) {
    statuses.push((await bob.post(`/api/rooms/${roomId}/periods`, { grain: "day", anchor: daysAgo(n) })).status);
  }
  expect(statuses.every((s) => s === 200 || s === 202)).toBe(true);
  expect(statuses.filter((s) => s === 202).length).toBeGreaterThanOrEqual(5);
});

test("15. a one-minute schedule is accepted, and its run cannot start another run (A3, B7)", async () => {
  test.setTimeout(330000); // waits for a real scheduler tick
  const alice = await api("alice");
  const made = await alice.post("/api/rooms", {
    kind: "report",
    title: "Self-starting",
    instructions: "Summarise the sales figures, then start another run",
    scheduleText: "every 1 minute",
    tz: "UTC",
    defaultGrain: "day",
  });
  expect(made.status).toBe(201);
  const id = made.body.id as string;
  try {
    // Due within a minute, picked up by the scheduler's once-a-minute poll: up to
    // about two minutes. Its run asks for room_run on itself.
    await expect.poll(async () => (await editions("alice", id)).length, { timeout: 240000, intervals: [5000] }).toBeGreaterThan(0);
    const room0 = (await alice.get(`/api/rooms/${id}`)).body as { rev: number };
    await alice.patch(`/api/rooms/${id}`, { expectedRev: room0.rev, changes: { paused: true } });
    await new Promise((r) => setTimeout(r, 10000));
    const made1 = await editions("alice", id);
    // Had the run's room_run worked there would be a second, run-now edition at once.
    expect(made1.length).toBe(1);
    expect(made1[0].status).not.toBe("building");
  } finally {
    await alice.post(`/api/rooms/${id}/archive`);
  }
});

test.afterAll(async () => {
  // Leave the stack tidy for the next run: archive what this flow made.
  const p = people();
  if (roomId) await (await api("alice")).post(`/api/rooms/${roomId}/archive`);
  void p;
});

/** Bob's main-conversation notes that link this workspace. */
async function notesFor(page: Page, id: string): Promise<number> {
  const res = await page.request.get("/api/chat/history?limit=200");
  const messages = ((await res.json()).messages ?? []) as Array<{ role: string; content: string }>;
  return messages.filter((m) => m.role === "assistant" && m.content.includes(`room=${id}`)).length;
}

/** Run the workspace now and wait for one more published edition. */
async function runAndPublish(id: string) {
  const before = (await editions("alice", id)).filter((e) => e.status === "published").length;
  const run = await (await api("alice")).post(`/api/rooms/${id}/run`, {});
  expect([200, 202]).toContain(run.status);
  await expect
    .poll(async () => (await editions("alice", id)).filter((e) => e.status === "published").length, {
      timeout: 60000,
    })
    .toBeGreaterThan(before);
}

test("16. a workspace Bob muted leaves no note in his main conversation (C7)", async () => {
  const bob = await api("bob");
  expect((await bob.put(`/api/rooms/${roomId}/notify`, { inApp: true, muted: true, channels: {} })).status).toBe(200);
  const before = await notesFor(bobPage, roomId);
  await runAndPublish(roomId);
  await bobPage.waitForTimeout(3000);
  expect(await notesFor(bobPage, roomId)).toBe(before);
});

test("17. with in-app off, Bob gets no note in his main conversation (C7)", async () => {
  expectedToFailUntil("the runs lane: fan-out reads the stored in-app choice (in progress)");
  const bob = await api("bob");
  expect((await bob.put(`/api/rooms/${roomId}/notify`, { inApp: false, muted: false, channels: {} })).status).toBe(200);
  const before = await notesFor(bobPage, roomId);
  await runAndPublish(roomId);
  await bobPage.waitForTimeout(3000);
  expect(await notesFor(bobPage, roomId)).toBe(before);
});
