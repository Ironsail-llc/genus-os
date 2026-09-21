/** Browser-only deployment review; every API call is intercepted. */
import { test, expect } from "@playwright/test";

test("reviews a release, commits it, and restores a cancelled rollback", async ({ page }, testInfo) => {
  const release = "a".repeat(64);
  const id = "00000000-0000-4000-8000-000000000001";
  const rollbackId = "00000000-0000-4000-8000-000000000002";
  const stages = ["plan", "scout", "research", "qualify", "contacts", "verify", "promotion", "draft", "conversation", "activation", "delivery", "stop", "inbox", "reconcile", "business"];
  const fleet = { release_id: release, platform_revision: "b".repeat(40), agents: ["scout", "researcher", "qualifier", "contact-finder", "sdr"], workflows: stages.map((stage) => `sales-${stage}`), plugins: [{ name: "business-adapter", version: "1.0" }] };
  const empty = { release_id: null, agents: [], workflows: [], plugins: [] };
  const prepared = { id, status: "preparing", direction: "deploy", source_release_id: null, target_release_id: release, source: empty, target: fleet, actor: "operator:human", reason: "Install the reviewed sales release" };
  const committed = { ...prepared, status: "committed" };
  const rollback = { ...prepared, id: rollbackId, direction: "rollback", source_release_id: release, target_release_id: null, source: fleet, target: empty };
  let selected: string | null = null;
  let pending: typeof prepared | typeof rollback | null = null;
  let history: object[] = [];
  const writes: { path: string; body: unknown }[] = [];
  const salesWrites = () => writes.filter((write) => write.path.startsWith("/api/sales/") || write.path.startsWith("/api/bridge/api/sales"));
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let body: unknown = {};
    if (["POST", "PATCH"].includes(request.method())) writes.push({ path, body: request.postDataJSON() });
    if (path === "/api/auth/session") body = { user: { name: "Operator", email: "operator@example.com" }, role: "owner", expires: "2099-01-01T00:00:00Z" };
    else if (path === "/api/bridge/api/sales") body = { prospects: [], actions: [], jobs: [], budgets: [], settings: {} };
    else if (path === "/api/sales/deployment") body = { configured: true, settings_revision: selected ? 2 : 1, selected_release_id: selected, pending, history,
      rollback_candidate: selected ? committed : null, control_busy: false, runtime: { ready: !pending, reason: pending ? "Deployment awaits verified commit or restoration" : null } };
    else if (path.includes("/releases/")) body = { ...fleet, name: "Example sales fleet", version: "candidate-1", source_revision: "c".repeat(40) };
    else if (path.endsWith("/prepare")) { pending = prepared; body = prepared; }
    else if (path.endsWith("/commit")) { selected = release; pending = null; history = [committed]; body = committed; }
    else if (path.endsWith("/rollback")) { pending = rollback; body = rollback; }
    else if (path.endsWith("/abort")) { pending = null; const aborted = { ...rollback, status: "aborted" }; history = [aborted, committed]; body = aborted; }
    else if (path === "/api/chat/history") body = { messages: [] };
    else if (path === "/api/health") body = { status: "ok", services: [] };
    else if (path.includes("/status")) body = { active: false, plan: null, deep: null };
    else if (path === "/api/events/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: ping\ndata: {}\n\n" });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/?v=sales", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Manage sales deployment" }).click();
  await expect(page.getByText("No fleet selected")).toBeVisible();
  await page.getByLabel("Release fingerprint").fill(release);
  await expect(page.getByRole("button", { name: "Prepare installation" })).toBeDisabled();
  await page.getByRole("button", { name: "Inspect release" }).click();
  await expect(page.getByText("candidate-1", { exact: true })).toBeVisible();
  await page.getByLabel("Reason for this change").fill(prepared.reason);
  await page.getByRole("button", { name: "Prepare installation" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("release-inspection.png"), fullPage: true });
  await page.getByRole("button", { name: "Prepare installation" }).click();
  await expect(page.getByRole("button", { name: "Install reviewed release" })).toBeVisible();
  expect(salesWrites()).toHaveLength(1);
  await page.getByRole("button", { name: "Install reviewed release" }).click();
  await expect(page.getByText("Runtime verified", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Prepare rollback" }).click();
  await expect(page.getByText("Pending rollback", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Apply reviewed rollback" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("rollback-mobile.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByLabel("Reason for this change").fill("Keep the current release after inspection");
  await page.getByRole("button", { name: "Restore and cancel" }).click();
  await expect(page.getByText("Deployment aborted.", { exact: true })).toBeVisible();
  expect(salesWrites()).toEqual([
    { path: "/api/sales/deployment/prepare", body: { release_id: release, expected_revision: 1, reason: prepared.reason } },
    { path: `/api/sales/deployment/transitions/${id}/commit`, body: {} },
    { path: `/api/sales/deployment/transitions/${id}/rollback`, body: { expected_revision: 2, reason: prepared.reason } },
    { path: `/api/sales/deployment/transitions/${rollbackId}/abort`, body: { reason: "Keep the current release after inspection" } },
  ]);
  for (const write of writes.filter((entry) => !salesWrites().includes(entry))) {
    expect(write.path).toBe("/api/actions/execute");
    expect(["list_tasks", "agent_status"]).toContain((write.body as { tool: string }).tool);
  }
  expect(errors).toEqual([]);
});
