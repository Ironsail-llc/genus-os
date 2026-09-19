/** Qualification judgments and corrections; no real sales API calls. */
import { test, expect } from "@playwright/test";

test("reviews a frozen dossier independently and preserves the original assessment", async ({ page }, testInfo) => {
  const api = "/api/bridge/api/sales/calibration";
  const cohort = { id: "cohort-1", name: "Pilot sample", target_size: 100, agreement_target_percent: 85, policy_versions: { network_access: "v1" } };
  const hash = "a".repeat(64);
  const assessments: { id: string; revision: number; reference_decision: string; reason: string; actor: string }[] = [];
  const item = { id: "item-1", ordinal: 1, name: "Example Clinic", domain: "clinic.example.com", buying_case: "network_access", policy_version: "v1", snapshot_hash: hash,
    snapshot: { name: "Example Clinic", domain: "clinic.example.com", version: 2, qualification: { decision: "rejected", score: 40 },
      policy: { version: "v1", buying_case: "network_access", weights: { prescribing: 100 }, required: ["prescribing"], threshold: 80, max_evidence_age_days: 90, criteria_definitions: { prescribing: "Explicit prescribing service evidence." } },
      dossier: { summary: "Publicly described prescription services.", unanswered: ["Purchasing volume unknown"], evidence: [{ id: "service", field: "prescribing", value: true, confidence: "supported", url: "https://clinic.example.com/services", excerpt: "Prescription management services" }] } } };
  function metrics(decision?: string) {
    return { enrolled: 1, reviewed: decision ? 1 : 0, agreements: decision === "rejected" ? 1 : 0, agreement_percent: decision ? (decision === "rejected" ? 100 : 0) : null,
      uncertain_reference: 0, reference_qualified: decision === "qualified" ? 1 : 0, reference_rejected: decision === "rejected" ? 1 : 0,
      model_needs_research: 0, false_positives: 0, missed_fits: decision === "qualified" ? 1 : 0, qualified_precision_percent: null };
  }
  const writes: { path: string; body: unknown }[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    let body: unknown = {};
    if (["POST", "PATCH"].includes(request.method())) writes.push({ path, body: request.postDataJSON() });
    if (path === "/api/auth/session") body = { user: { name: "Operator", email: "operator@example.com" }, role: "owner", expires: "2099-01-01T00:00:00Z" };
    else if (path === "/api/bridge/api/sales") body = { prospects: [], actions: [], jobs: [], budgets: [], settings: {} };
    else if (path === "/api/bridge/api/sales/settings") body = { config: { active_policy_versions: cohort.policy_versions }, revision: 5 };
    else if (path === api) body = { items: [cohort], next_cursor: null };
    else if (path === `${api}/cohort-1`) {
      const initial = metrics(assessments[0]?.reference_decision); const latest = metrics(assessments.at(-1)?.reference_decision);
      body = { cohort, initial, latest, corrections: assessments.length > 1 ? 1 : 0, review_complete: false, agreement_target_met: false,
        by_buying_case: { network_access: { policy_version: "v1", initial, latest } }, limitations: ["The sample is not randomized."] };
    }
    else if (path === `${api}/cohort-1/items`) body = { items: [item], next_cursor: null };
    else if (path === `${api}/items/item-1`) body = { ...item, assessments };
    else if (path === `${api}/items/item-1/assessment`) {
      const input = request.postDataJSON();
      const row = { id: `assessment-${assessments.length + 1}`, revision: assessments.length + 1, reference_decision: input.reference_decision, reason: input.reason, actor: "operator:reviewer" };
      assessments.push(row); body = row;
    }
    else if (path === "/api/chat/history") body = { messages: [] };
    else if (path === "/api/health") body = { status: "ok", services: [] };
    else if (path.includes("/status")) body = { active: false, plan: null, deep: null };
    else if (path === "/api/events/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: ping\ndata: {}\n\n" });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/?v=sales", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Assess qualification quality" }).click();
  await page.getByRole("button", { name: "Open Pilot sample" }).click();
  await page.getByRole("button", { name: "Review Example Clinic" }).click();
  const dossier = page.getByRole("article", { name: "Frozen qualification dossier" });
  await expect(dossier.getByText("Prescription management services")).toBeVisible();
  await expect(dossier.getByText(/Model decision:/)).toHaveCount(0);
  await dossier.getByText("Frozen policy rules", { exact: true }).click();
  await expect(dossier.getByText("Frozen policy snapshot")).toBeVisible();
  await page.getByLabel("Your qualification assessment").selectOption("qualified");
  await page.getByLabel("Assessment reason").fill("Reviewed explicit prescription service evidence");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Record assessment", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("qualification-mobile.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Record assessment", exact: true }).click();
  await expect(dossier.getByText("Model decision: rejected · 40/100")).toBeVisible();
  await expect(dossier.getByText(/Assessment 1: qualified/)).toBeVisible();
  await page.getByLabel("Your qualification assessment").selectOption("rejected");
  await page.getByLabel("Assessment reason").fill("Corrected after checking the cited service scope");
  await page.getByRole("button", { name: "Record correction" }).click();
  await expect(dossier.getByText(/Assessment 1: qualified/)).toBeVisible();
  await expect(dossier.getByText(/Assessment 2: rejected/)).toBeVisible();
  await expect(page.getByText("Initial agreement target not yet met.", { exact: false })).toBeVisible();
  await page.getByText("Latest assessments · 1 corrected dossiers", { exact: true }).click();
  const latest = page.locator("details").filter({ has: page.getByText("Latest assessments · 1 corrected dossiers", { exact: true }) });
  await expect(latest.getByText("100.0%", { exact: true })).toBeVisible();
  const salesWrites = writes.filter((row) => row.path.includes("/sales"));
  expect(salesWrites).toEqual([
    { path: `${api}/items/item-1/assessment`, body: { expected_snapshot_hash: hash, expected_assessment_id: null, reference_decision: "qualified", reason: "Reviewed explicit prescription service evidence" } },
    { path: `${api}/items/item-1/assessment`, body: { expected_snapshot_hash: hash, expected_assessment_id: "assessment-1", reference_decision: "rejected", reason: "Corrected after checking the cited service scope" } },
  ]);
  expect(errors).toEqual([]);
});
