/**
 * Creating a workspace from the dashboard, and using one at phone width,
 * against the real stack: the dialog posts to the real bridge, which checks
 * the agent against the company's engine; at 390 px the rail collapses and
 * the workspace's conversation still works with no sideways scroll.
 */
import { expect, test } from "@playwright/test";
import { api, requireStack, roomLink, say, signedIn, unique } from "./stack";

requireStack();

const ROOM_ID = /room=([0-9a-f-]{36})/;

test("F1. Carol starts a workspace with an agent from the dashboard; an unknown agent is refused", async ({ browser }) => {
  const { page } = await signedIn(browser, "carol");
  await page.goto("/?v=chat");
  // Signed in as Carol, and the page has settled, before looking for the control.
  await expect(page.getByTestId("chat-landing-greeting")).toContainText("Carol", { timeout: 30000 });
  await page.getByTestId("room-rail").getByTestId("new-workspace-button").click({ timeout: 15000 });
  const dialog = page.getByTestId("new-workspace-dialog");
  await expect(dialog.getByTestId("new-workspace-agent")).not.toHaveValue("", { timeout: 15000 });
  const title = unique("Carol's pricing questions");
  await dialog.getByTestId("new-workspace-title").fill(title);
  await dialog.getByTestId("new-workspace-create").click();
  await expect(page).toHaveURL(ROOM_ID, { timeout: 30000 });
  const id = ROOM_ID.exec(page.url())?.[1] ?? "";
  const carol = await api("carol");
  try {
    await expect(page.getByTestId("room-view")).toBeVisible({ timeout: 30000 });
    const created = await carol.get(`/api/rooms/${id}`);
    expect(created.body).toMatchObject({ kind: "agent", title, access: "assigned" });
    const listed = (await carol.get("/api/rooms")).body.rooms as Array<{ id: string }>;
    expect(listed.map((r) => r.id)).toContain(id);

    const refused = await carol.post("/api/rooms", { kind: "agent", title: unique("Nobody"), agentId: "no-such-agent" });
    expect(refused.status).toBe(422);
    expect(refused.body.detail ?? refused.body).toMatchObject({ field: "agentId" });
  } finally {
    await carol.post(`/api/rooms/${id}/archive`);
  }
});

test("C8. at phone width the rail collapses and a workspace's conversation works", async ({ browser }) => {
  const carol = await api("carol");
  const id = (await carol.post("/api/rooms", { kind: "agent", title: unique("On the phone") })).body.id as string;
  try {
    const { page } = await signedIn(browser, "carol", { width: 390, height: 844 });
    const overflows = () => page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    await page.goto("/?v=chat");
    const rail = page.getByTestId("room-rail");
    await expect(rail.getByTestId("room-rail-toggle")).toBeVisible({ timeout: 30000 });
    await expect(rail.getByRole("navigation", { name: "Workspaces" })).toHaveCount(0);
    expect(await overflows()).toBe(false);

    await page.goto(roomLink(id));
    await expect(page.getByTestId("room-view")).toBeVisible({ timeout: 30000 });
    await say(page, "hello from my phone");
    await expect(page.getByTestId("room-view").getByText("Answering Carol: hello from my phone")).toBeVisible({
      timeout: 30000,
    });
    expect(await overflows()).toBe(false);
  } finally {
    await carol.post(`/api/rooms/${id}/archive`);
  }
});
