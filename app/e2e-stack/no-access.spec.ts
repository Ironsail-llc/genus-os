/**
 * No access, against the real stack: Carol, in the same company but shared
 * nothing, opens a workspace's link and sees that it is not available; every
 * route answers as if it did not exist (404), so its existence cannot be probed.
 */
import { expect, test } from "@playwright/test";
import { api, requireStack, roomLink, signedIn, unique } from "./stack";

requireStack();

test("Carol opens a link to a workspace she has no access to: not found (G2, H4)", async ({ browser }) => {
  const alice = await api("alice");
  const id = (await alice.post("/api/rooms", { kind: "agent", title: unique("Private") })).body.id as string;
  try {
    const { page } = await signedIn(browser, "carol");
    await page.goto(roomLink(id));
    await expect(page.getByTestId("room-unavailable")).toBeVisible({ timeout: 30000 });
    await expect(page.getByText(/Private/)).toHaveCount(0);

    const carol = await api("carol");
    expect((await carol.get(`/api/rooms/${id}`)).status).toBe(404);
    expect((await carol.get(`/api/rooms/${id}/editions`)).status).toBe(404);
    expect((await carol.post(`/api/rooms/${id}/run`, {})).status).toBe(404);
    const history = await page.request.get(`/api/chat/history?room=${id}`);
    expect(history.status()).toBe(404);
  } finally {
    await alice.post(`/api/rooms/${id}/archive`);
  }
});
