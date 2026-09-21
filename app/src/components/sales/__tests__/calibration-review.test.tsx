import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CalibrationReview } from "../calibration-review";

const cohort = { id: "cohort-1", name: "Pilot review", target_size: 100, agreement_target_percent: 85, policy_versions: { network_access: "v1" } };
const summary = { enrolled: 1, reviewed: 0, agreement_percent: null, uncertain_reference: 0, false_positives: 0, missed_fits: 0, reference_qualified: 0, reference_rejected: 0, model_needs_research: 0, qualified_precision_percent: null };
const item = { id: "item-1", ordinal: 1, name: "Example Clinic", domain: "clinic.example.com", buying_case: "network_access", policy_version: "v1", snapshot_hash: "a".repeat(64),
  snapshot: { name: "Example Clinic", domain: "clinic.example.com", version: 2, qualification: { decision: "rejected", score: 40 }, policy: { version: "v1", weights: { prescribing: 100 }, required: ["prescribing"], threshold: 80, max_evidence_age_days: 90, buying_case: "network_access" },
    dossier: { summary: "Prescription services described publicly.", unanswered: ["Purchasing role unknown"], evidence: [{ id: "service", field: "prescribing", value: true, confidence: "supported", url: "https://clinic.example.com/services", excerpt: "Prescription care" }] } }, assessments: [] };
afterEach(() => vi.restoreAllMocks());
function backend(fail = false) {
  let assessments: object[] = [];
  return vi.spyOn(global, "fetch").mockImplementation(async (url, options) => {
    const path = String(url);
    if (path.endsWith("/assessment")) {
      if (fail) return Response.json({}, { status: 409 });
      const body = JSON.parse(String(options?.body));
      const row = { id: "assessment-1", revision: 1, reference_decision: body.reference_decision, reason: body.reason, actor: "operator:reviewer" };
      assessments = [row]; return Response.json(row);
    }
    if (path.endsWith("/settings")) return Response.json({ config: { active_policy_versions: cohort.policy_versions }, revision: 5 });
    if (path.endsWith("/items/item-1")) return Response.json({ ...item, assessments });
    if (path.endsWith("/items")) return Response.json({ items: [item], next_cursor: null });
    if (path.endsWith("/cohort-1")) return Response.json({ cohort, initial: summary, latest: summary, corrections: 0, review_complete: false, agreement_target_met: false,
      by_buying_case: { network_access: { policy_version: "v1", initial: summary, latest: summary } }, limitations: ["Not a randomized sample."] });
    if (options?.method === "POST") return Response.json(cohort);
    return Response.json({ items: [cohort], next_cursor: null });
  });
}
async function openItem() {
  fireEvent.click(await screen.findByRole("button", { name: "Open Pilot review" }));
  fireEvent.click(await screen.findByRole("button", { name: "Review Example Clinic" }));
  await screen.findByText("Prescription care");
}
it("records an independent human fit judgment before showing the model result", async () => {
  const fetcher = backend(); render(<CalibrationReview />); await openItem();
  expect(screen.queryByText("Model decision: rejected · 40/100")).not.toBeInTheDocument();
  expect(screen.getByText("Frozen policy snapshot")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Record assessment" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Your qualification assessment"), { target: { value: "qualified" } });
  fireEvent.change(screen.getByLabelText("Assessment reason"), { target: { value: "Reviewed explicit prescription service evidence" } });
  fireEvent.click(screen.getByRole("button", { name: "Record assessment" }));
  await screen.findByText("Model decision: rejected · 40/100");
  const writes = fetcher.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(writes).toHaveLength(1);
  expect(String(writes[0][0])).toContain("/calibration/items/item-1/assessment");
  expect(JSON.parse(String(writes[0][1]?.body))).toEqual({ expected_snapshot_hash: item.snapshot_hash, expected_assessment_id: null, reference_decision: "qualified", reason: "Reviewed explicit prescription service evidence" });
  expect(fetcher.mock.calls.some(([url]) => String(url).includes("/decision") || String(url).includes("/prospects/"))).toBe(false);
});
it("holds a stale assessment until reloading the item and never retries it", async () => {
  const fetcher = backend(true); render(<CalibrationReview />); await openItem();
  fireEvent.change(screen.getByLabelText("Your qualification assessment"), { target: { value: "rejected" } });
  fireEvent.change(screen.getByLabelText("Assessment reason"), { target: { value: "Reviewed contradictory source evidence" } });
  fireEvent.click(screen.getByRole("button", { name: "Record assessment" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/reload/i);
  expect(screen.getByRole("button", { name: "Record assessment" })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
});
it("creates the 100-dossier pilot cohort against the displayed policy settings", async () => {
  const fetcher = backend(); render(<CalibrationReview />);
  await screen.findByRole("button", { name: "Open Pilot review" });
  fireEvent.change(screen.getByLabelText("Review cohort name"), { target: { value: "First pilot" } });
  fireEvent.change(screen.getByLabelText("Cohort review purpose"), { target: { value: "Measure the initial qualification accuracy" } });
  fireEvent.click(screen.getByRole("button", { name: "Create 100-dossier cohort" }));
  await waitFor(() => expect(fetcher.mock.calls.some(([, options]) => options?.method === "POST")).toBe(true));
  const write = fetcher.mock.calls.find(([, options]) => options?.method === "POST");
  expect(JSON.parse(String(write?.[1]?.body))).toEqual({ name: "First pilot", reason: "Measure the initial qualification accuracy", target_size: 100, agreement_target_percent: 85, expected_settings_revision: 5 });
});
