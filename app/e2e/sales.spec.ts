/** Native Helm review and read recovery with all API traffic intercepted. */
import { test, expect } from "@playwright/test";

test("reviews a practice identity and recovers a read without outbound writes", async ({ page }, testInfo) => {
  const writes: { path: string; body: unknown }[] = [];
  let matched = false;
  let retried = false;
  const customer = { id: "00000000-0000-4000-8000-000000000001", name: "Example Customer", domain: "example.com", version: 1, owner: "human_review", status: "qualified" };
  const practice = { id: "00000000-0000-4000-8000-000000000002", source: "orders_app", account_id: "account-1", external_id: "practice-1", revision: "reviewed-v1",
    observed_at: "2026-09-18T12:00:00Z", data: { name: "East Clinic", state: "TX", active: true, business_unit_id: "group-1" } };
  const job = { id: "00000000-0000-4000-8000-000000000003", kind: "sales.business", status: "failed", error: "Business provider page requires review",
    attempts: 5, max_attempts: 5, updated_at: "2026-09-18T12:00:00Z", scope: { source: "orders_app", account_id: "account-1", kind: "practice" } };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let body: unknown = {};
    if (request.method() === "POST" || request.method() === "PATCH") writes.push({ path, body: request.postDataJSON() });
    if (path === "/api/auth/session") body = { user: { name: "Operator", email: "operator@example.com" }, role: "owner", expires: "2099-01-01T00:00:00Z" };
    else if (path === "/api/bridge/api/sales") body = { prospects: [customer], actions: [], jobs: [], budgets: [], settings: { business_sources: [{ source: "orders_app", account_id: "account-1" }] } };
    else if (path.endsWith("/business-observations")) body = { items: [{ ...practice, prospect_id: matched ? customer.id : null, binding_status: matched ? "confirmed" : null }], next_cursor: null };
    else if (path.endsWith("/business-customer")) { matched = true; body = { ok: true }; }
    else if (path.endsWith("/provider-reads")) body = { items: retried ? [] : [job], next_cursor: null };
    else if (path.endsWith("/retry")) { retried = true; body = { ok: true }; }
    else if (path === "/api/chat/history") body = { messages: [] };
    else if (path === "/api/health") body = { status: "ok", services: [] };
    else if (path.includes("/status")) body = { active: false, plan: null, deep: null };
    else if (path === "/api/events/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: ping\ndata: {}\n\n" });
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/?v=sales", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Review imported practices" }).click();
  await page.getByRole("button", { name: "Review match for East Clinic" }).click();
  await page.getByLabel("Genus customer", { exact: true }).selectOption(customer.id);
  await page.getByLabel("Matching evidence and reason").fill("Verified the practice location and company ownership");
  await expect(page.getByRole("button", { name: "Confirm practice match" })).toBeDisabled();
  await page.getByLabel("I verified that this practice belongs to this customer").check();
  await page.screenshot({ path: testInfo.outputPath("practice-review.png"), fullPage: true });
  await page.getByRole("button", { name: "Confirm practice match" }).click();
  await expect(page.getByRole("status")).toHaveText("Practice match saved.");
  await page.getByRole("button", { name: "Inspect provider reads" }).click();
  await page.getByRole("button", { name: "Review read recovery" }).click();
  await page.getByLabel("What was repaired?").fill("Repaired source account configuration and checked access");
  await page.getByRole("button", { name: "Retry this read" }).click();
  await expect(page.getByText("Read queued for retry.")).toBeVisible();
  const salesWrites = writes.filter((write) => write.path.startsWith("/api/bridge/api/sales"));
  expect(salesWrites).toEqual([
    { path: `/api/bridge/api/sales/prospects/${customer.id}/business-customer`, body: { observation_id: practice.id, expected_revision: "reviewed-v1", reason: "Verified the practice location and company ownership" } },
    { path: `/api/bridge/api/sales/jobs/${job.id}/retry`, body: { reason: "Repaired source account configuration and checked access" } },
  ]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath("sales-mobile.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
