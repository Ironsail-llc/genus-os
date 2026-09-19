/** Exact-content publication review; all API traffic is intercepted. */
import { test, expect } from "@playwright/test";

test("uploads and reviews a claim packet, then confirms an uncertain publication", async ({ page }, testInfo) => {
  const packet = { kind: "knowledge", version: "claims-1", data: { claims: { workflow: "One connected ordering workflow." }, sources: [{ url: "https://example.com/services", checked_at: "2026-09-19" }] } };
  const hash = "a".repeat(64);
  const record = { ...packet, approved_by: "operator:reviewer", approved_at: "2026-09-19T00:00:00Z", content_hash: hash };
  let published = false;
  const writes: { path: string; body: unknown }[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    let body: unknown = {}; let status = 200;
    if (["POST", "PATCH"].includes(request.method())) writes.push({ path, body: request.postDataJSON() });
    if (path === "/api/auth/session") body = { user: { name: "Operator", email: "operator@example.com" }, role: "owner", expires: "2099-01-01T00:00:00Z" };
    else if (path === "/api/bridge/api/sales") body = { prospects: [], actions: [], jobs: [], budgets: [], settings: {} };
    else if (path === "/api/bridge/api/sales/settings") body = { config: {}, revision: 0 };
    else if (path === "/api/bridge/api/sales/library") body = { items: published && url.searchParams.get("kind") === "knowledge" ? [record] : [], next_cursor: null };
    else if (path.endsWith("/library/preview")) body = { packet, content_hash: hash };
    else if (path.endsWith("/library/publication")) { published = true; status = 502; body = { error: "Publication response lost" }; }
    else if (path.includes("/library/records/")) body = record;
    else if (path === "/api/chat/history") body = { messages: [] };
    else if (path === "/api/health") body = { status: "ok", services: [] };
    else if (path.includes("/status")) body = { active: false, plan: null, deep: null };
    else if (path === "/api/events/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: ping\ndata: {}\n\n" });
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/?v=sales", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Review sales library" }).click();
  await page.getByLabel("Library review packet").setInputFiles({ name: "claims.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(packet)) });
  await expect(page.getByText("One connected ordering workflow.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Publish reviewed version" })).toBeDisabled();
  await page.getByLabel("Publication review reason").fill("Reviewed current business sources and limitations");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Publish reviewed version" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("publication-review.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Publish reviewed version" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("publication-mobile.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Publish reviewed version" }).click();
  await expect(page.getByRole("region", { name: "Publish sales library", exact: true }).getByRole("alert")).toContainText("Check the published version");
  await page.getByRole("button", { name: "Check published version" }).click();
  await expect(page.getByRole("status")).toContainText("Publication confirmed");
  await expect(page.getByLabel("Active claim library")).toHaveValue("");
  const salesWrites = writes.filter((write) => write.path.includes("/sales"));
  expect(salesWrites).toEqual([
    { path: "/api/bridge/api/sales/library/preview", body: packet },
    { path: "/api/bridge/api/sales/library/publication", body: { packet, expected_hash: hash, reason: "Reviewed current business sources and limitations" } },
  ]);
  for (const write of writes.filter((entry) => !entry.path.includes("/sales"))) {
    expect(write.path).toBe("/api/actions/execute");
    expect(["list_tasks", "agent_status"]).toContain((write.body as { tool: string }).tool);
  }
  expect(errors).toEqual([]);
});
