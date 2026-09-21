/** Library selection UI only; all API traffic is intercepted. */
import { test, expect } from "@playwright/test";

test("inspects published policy and claim contents before selecting the library", async ({ page }, testInfo) => {
  let config = { active_policy_versions: {}, active_knowledge_version: "", sending_enabled: false };
  const policy = { kind: "qualification", version: "pilot-1", approved_by: "operator:reviewer", approved_at: "2026-09-19T00:00:00Z",
    data: { buying_case: "network_access", required: ["prescribing"], weights: { prescribing: 80, multiple_locations: 20 }, threshold: 80, max_evidence_age_days: 90 } };
  const knowledge = { ...policy, kind: "knowledge", version: "claims-1", data: { claims: { access: "Access participating pharmacy partners through one ordering workflow." }, source: "https://example.com/services" } };
  const writes: { path: string; body: unknown }[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    let body: unknown = {};
    if (["POST", "PATCH"].includes(request.method())) writes.push({ path, body: request.postDataJSON() });
    if (path === "/api/auth/session") body = { user: { name: "Operator", email: "operator@example.com" }, role: "owner", expires: "2099-01-01T00:00:00Z" };
    else if (path === "/api/bridge/api/sales") body = { prospects: [], actions: [], jobs: [], budgets: [], settings: config };
    else if (path === "/api/bridge/api/sales/settings") body = { config, revision: 1 };
    else if (path === "/api/bridge/api/sales/library") body = { items: [url.searchParams.get("kind") === "qualification" ? policy : knowledge], next_cursor: null };
    else if (path === "/api/bridge/api/sales/library/selection") {
      const submitted = request.postDataJSON();
      config = { ...config, active_policy_versions: submitted.policy_versions, active_knowledge_version: submitted.knowledge_version };
      body = { config, revision: 2 };
    } else if (path === "/api/chat/history") body = { messages: [] };
    else if (path === "/api/health") body = { status: "ok", services: [] };
    else if (path.includes("/status")) body = { active: false, plan: null, deep: null };
    else if (path === "/api/events/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: ping\ndata: {}\n\n" });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/?v=sales", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Review sales library" }).click();
  await page.getByLabel("Qualification for network access").selectOption("pilot-1");
  await page.getByLabel("Active claim library").selectOption("claims-1");
  await page.getByLabel("Reason for library selection").fill("Reviewed qualification rules and published claims");
  await page.getByRole("button", { name: "Review library selection" }).click();
  const preview = page.getByRole("region", { name: "Review selected library" });
  await expect(preview).toContainText("prescribing: 80 points · required");
  await expect(preview).toContainText(knowledge.data.claims.access);
  expect(writes.filter((write) => write.path.includes("/sales"))).toHaveLength(0);
  await page.getByRole("button", { name: "Use reviewed library" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("library-review.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Use reviewed library" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("library-mobile.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Use reviewed library" }).click();
  await expect(page.getByRole("status")).toHaveText("Reviewed library selected.");
  expect(writes.filter((write) => write.path.includes("/sales"))).toEqual([{
    path: "/api/bridge/api/sales/library/selection", body: { policy_versions: { network_access: "pilot-1" }, knowledge_version: "claims-1", expected_revision: 1, reason: "Reviewed qualification rules and published claims" },
  }]);
  expect(config.sending_enabled).toBe(false);
  expect(errors).toEqual([]);
});
