/**
 * The company admin and the parent-company admin, against the real stack.
 * Dana sees every workspace of her company (archived ones too), reassigns
 * one and restores an archived one. Hal, an admin of the parent company,
 * sees none of the company's workspaces until he switches into it.
 */
import { expect, test } from "@playwright/test";
import { api, people, requireStack, signedIn, unique } from "./stack";

requireStack();
test.describe.configure({ mode: "serial" });

let kept = "";
let archived = "";

test.beforeAll(async () => {
  const alice = await api("alice");
  kept = (await alice.post("/api/rooms", { kind: "agent", title: unique("Pricing") })).body.id as string;
  archived = (await alice.post("/api/rooms", { kind: "agent", title: unique("Old pricing") })).body.id as string;
  expect((await alice.post(`/api/rooms/${archived}/archive`)).status).toBe(200);
});

test.afterAll(async () => {
  for (const id of [kept, archived]) if (id) await (await api("dana")).post(`/api/rooms/${id}/archive`);
});

test("Dana sees every company workspace, archived ones included (G4, A6)", async ({ browser }) => {
  const { page } = await signedIn(browser, "dana");
  await page.goto("/?v=chat");
  await page.getByTestId("rooms-admin-entry").click();
  const admin = page.getByTestId("rooms-admin");
  await expect(admin.getByTestId(`rooms-admin-row-${kept}`)).toBeVisible({ timeout: 30000 });
  await expect(admin.getByTestId(`rooms-admin-row-${archived}`)).toBeVisible();
});

test("Dana reassigns a workspace to Bob: he is assigned and it runs as him (G4)", async ({ browser }) => {
  const p = people();
  const { page } = await signedIn(browser, "dana");
  await page.goto("/?v=chat");
  await page.getByTestId("rooms-admin-entry").click();
  await page.getByTestId(`rooms-admin-reassign-${kept}`).click();
  await page.getByTestId(`rooms-admin-reassign-to-${kept}`).selectOption(p.bob.id);
  await page.getByTestId(`rooms-admin-reassign-confirm-${kept}`).click();
  await expect
    .poll(async () => (await (await api("dana")).get(`/api/rooms/${kept}`)).body.runsAs, { timeout: 15000 })
    .toMatchObject({ userId: p.bob.id, displayName: "Bob" });
  const bobs = await (await api("bob")).get(`/api/rooms/${kept}`);
  expect(bobs.body).toMatchObject({ access: "assigned" });
});

test("Dana restores an archived workspace; it comes back paused (A6)", async ({ browser }) => {
  const { page } = await signedIn(browser, "dana");
  await page.goto("/?v=chat");
  await page.getByTestId("rooms-admin-entry").click();
  await page.getByTestId(`rooms-admin-restore-${archived}`).click();
  await expect
    .poll(async () => (await (await api("dana")).get(`/api/rooms/${archived}`)).body, { timeout: 15000 })
    .toMatchObject({ archived: false, paused: true });
});

test("Hal, a parent-company admin, sees the company's workspaces only after switching in (G5)", async () => {
  const p = people();
  const hal = await api("hal");
  const home = await hal.get("/api/rooms?scope=all");
  expect(home.status).toBe(200);
  const homeIds = (home.body.rooms as Array<{ id: string }>).map((r) => r.id);
  expect(homeIds).not.toContain(kept);
  expect((await hal.get(`/api/rooms/${kept}`)).status).toBe(404);

  await hal.switchTo(p.company);
  const inside = await hal.get("/api/rooms?scope=all");
  expect((inside.body.rooms as Array<{ id: string }>).map((r) => r.id)).toContain(kept);
  expect((await hal.get(`/api/rooms/${kept}`)).body).toMatchObject({ access: "admin" });
});
