/** Browser-only pilot configuration review; all API calls are intercepted. */
import { test, expect } from "@playwright/test";

test("reviews pilot limits before saving and preserves paused integrations", async ({ page }, testInfo) => {
  let config = { monthly_limit_units: 0, daily_limit_units: 0, timezone: "America/New_York", sending_enabled: false };
  const writes: { path: string; body: unknown }[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let body: unknown = {};
    if (["POST", "PATCH"].includes(request.method())) writes.push({ path, body: request.postDataJSON() });
    if (path === "/api/auth/session") body = { user: { name: "Operator", email: "operator@example.com" }, role: "owner", expires: "2099-01-01T00:00:00Z" };
    else if (path === "/api/bridge/api/sales") body = { prospects: [], actions: [], jobs: [], budgets: [], settings: config };
    else if (path === "/api/bridge/api/sales/settings") body = { config, revision: 1 };
    else if (path === "/api/bridge/api/sales/settings/review") {
      config = { ...config, ...request.postDataJSON().changes }; body = { config, revision: 2 };
    } else if (path === "/api/chat/history") body = { messages: [] };
    else if (path === "/api/health") body = { status: "ok", services: [] };
    else if (path.includes("/status")) body = { active: false, plan: null, deep: null };
    else if (path === "/api/events/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: ping\ndata: {}\n\n" });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/?v=sales", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Review pilot settings" }).click();
  await page.getByLabel("Monthly spending limit (USD)").fill("500");
  await page.getByLabel("Daily spending limit (USD)").fill("20");
  await page.getByLabel("Reason for these limits").fill("Reviewed initial pilot spending limits");
  await page.getByRole("button", { name: "Review changes" }).click();
  const preview = page.getByRole("region", { name: "Review pilot changes" });
  await expect(preview).toContainText("0 → 500");
  expect(writes.filter((write) => write.path.includes("/sales"))).toHaveLength(0);
  await page.getByRole("button", { name: "Save reviewed limits" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("limits-review.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Save reviewed limits" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("limits-mobile.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Save reviewed limits" }).click();
  await expect(page.getByRole("status")).toHaveText("Reviewed limits saved.");
  expect(writes.filter((write) => write.path.includes("/sales"))).toEqual([{
    path: "/api/bridge/api/sales/settings/review", body: { changes: { monthly_limit_units: 500_000_000, daily_limit_units: 20_000_000 }, expected_revision: 1, reason: "Reviewed initial pilot spending limits" },
  }]);
  for (const write of writes.filter((entry) => !entry.path.includes("/sales"))) {
    expect(write.path).toBe("/api/actions/execute");
    expect(["list_tasks", "agent_status"]).toContain((write.body as { tool: string }).tool);
  }
  expect(config.sending_enabled).toBe(false);
  expect(errors).toEqual([]);
});
