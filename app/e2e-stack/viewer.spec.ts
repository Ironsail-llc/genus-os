/**
 * A company viewer, against the real stack: Vera reads a workspace shared
 * with her, and can change nothing in it (D5, A1 negative, B6 negative).
 */
import { expect, test } from "@playwright/test";
import { api, people, requireStack, roomLink, signedIn, unique } from "./stack";

requireStack();

test("Vera reads a shared workspace and cannot post, run, edit or create (D5)", async ({ browser }) => {
  const p = people();
  const alice = await api("alice");
  const made = await alice.post("/api/rooms", {
    kind: "report",
    title: unique("Shared with Vera"),
    instructions: "Summarise the sales figures for the period with sample_sales_figures.",
    shareWith: [p.vera.id],
  });
  expect(made.status).toBe(201);
  const id = made.body.id as string;
  try {
    const { page } = await signedIn(browser, "vera");
    await page.goto(roomLink(id));
    const view = page.getByTestId("room-view");
    await expect(view.getByTestId("room-header")).toContainText("Shared with Vera", { timeout: 30000 });
    await expect(view.getByTestId("room-readonly-notice")).toContainText(/view-only/i);
    await expect(view.getByTestId("chat-input")).toBeDisabled();

    const vera = await api("vera");
    expect((await vera.get(`/api/rooms/${id}`)).body).toMatchObject({ access: "viewer" });
    expect((await vera.post(`/api/rooms/${id}/run`, {})).status).toBe(403);
    expect((await vera.patch(`/api/rooms/${id}`, { expectedRev: 1, changes: { title: "Mine" } })).status).toBe(403);
    expect((await vera.post("/api/rooms", { kind: "agent", title: "Mine" })).status).toBe(403);
  } finally {
    await alice.post(`/api/rooms/${id}/archive`);
  }
});
