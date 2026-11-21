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
import { api, expectedToFailUntil, people, requireStack, roomLink, say, signedIn } from "./stack";

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
  await say(alicePage, "Please create a daily sales report", { main: true });
  await expect(alicePage.getByText(/I will create a report workspace “Daily sales”/).last()).toBeVisible({ timeout: 30000 });
  await say(alicePage, "yes, create it. share with bob@example.com", { main: true });
  const answer = alicePage.getByText(/Open it: \/\?v=chat&room=/).last();
  await expect(answer).toBeVisible({ timeout: 30000 });
  roomId = ROOM_ID.exec((await answer.textContent()) ?? "")?.[1] ?? "";
  expect(roomId).not.toBe("");
  const made = await (await api("alice")).get(`/api/rooms/${roomId}`);
  expect(made.body).toMatchObject({ kind: "report", access: "assigned", settings: { schedule_text: "0 7 * * *", tz: "UTC" } });
  expect((made.body.members as Array<{ displayName: string }>).map((m) => m.displayName).sort()).toEqual(["Alice", "Bob"]);
});

test("1b. Alice previews it before it runs on schedule; only she sees the preview (A2)", async () => {
  await say(alicePage, `preview ${roomId}`, { main: true });
  await expect(alicePage.getByText(/Done \(room_run\)/).last()).toBeVisible({ timeout: 30000 });
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

test("3. run now builds an edition (B6)", async () => {
  const run = await (await api("bob")).post(`/api/rooms/${roomId}/run`, {});
  expect([200, 202]).toContain(run.status);
});

test("4. Bob sees the workspace in his rail with unread activity (C1, C6)", async () => {
  // Alice's turn in the workspace is activity Bob has not read.
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
  await expect(bobPage.getByText(/new edition/i).first()).toBeVisible({ timeout: 15000 });
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
  await bobPage.getByTestId("period-anchor").fill(yesterday());
  await bobPage.getByTestId("period-apply").click();
  await expect(bobPage.getByTestId("room-pinned-edition").getByTestId("edition-title")).toBeVisible({ timeout: 60000 });
});

test("9. Bob accepts a suggested task (E2)", async () => {
  expectedToFailUntil("lane 9: suggestions become linked tasks, and accepting assigns the task");
  const tasks = await (await api("bob")).get(`/api/rooms/${roomId}/tasks`);
  const suggested = (tasks.body.tasks as Array<{ taskId: string }>)[0];
  expect(suggested).toBeTruthy();
  const accepted = await (await api("bob")).post(`/api/rooms/${roomId}/tasks/${suggested.taskId}/accept`);
  expect(accepted.body).toMatchObject({ assignee: { displayName: "Bob" } });
});

test("10. Bob approves the action his own run proposed (E3)", async () => {
  expectedToFailUntil("lane 9: member approval");
  const approvals = await (await api("bob")).get("/api/approvals");
  expect(JSON.stringify(approvals.body)).toContain(roomId);
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

function yesterday(): string {
  return new Date(Date.now() - 86_400_000).toISOString().slice(0, 10);
}

test.afterAll(async () => {
  // Leave the stack tidy for the next run: archive what this flow made.
  const p = people();
  if (roomId) await (await api("alice")).post(`/api/rooms/${roomId}/archive`);
  void p;
});
