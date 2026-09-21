import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SalesReports } from "../sales-reports";
afterEach(() => vi.restoreAllMocks());
it("shows measured counts and unknown retention separately from proposals", async () => {
  vi.spyOn(global, "fetch").mockImplementation(async (url, options) => {
    expect(options?.method ?? "GET").toBe("GET");
    if (String(url).endsWith("/report-1")) return Response.json({ id: "report-1", status: "completed", prepared_at: "2026-09-19T12:00:00Z", dataset: { supporting_counts: { observed_businesses: 10, first_fulfilled: 2, eligible_30_day: 0, retained_30_day: null }, costs: { actual_units: 200000, reserved_units: 100000 }, population: { observed_businesses: 10, known_businesses: 10, complete: true }, cohorts: [], qualification_reviews: [], business_sources: [], limitations: ["History coverage incomplete"] }, analysis: { cohort_summary: "Two businesses have a fulfilled order.", limitations: ["No mature comparison group"], proposed_changes: [{ proposal: "Review the targeting criteria", metric_paths: ["supporting_counts.observed_businesses"] }] } });
    return Response.json({ items: [{ id: "report-1", period: "2026-W38", status: "completed" }], next_cursor: null });
  });
  render(<SalesReports />);
  fireEvent.click(await screen.findByRole("button", { name: /2026-W38/ }));
  await screen.findByText("Two businesses have a fulfilled order.");
  expect(screen.getByText("Unknown")).toBeInTheDocument();
  expect(screen.getByText("Review the targeting criteria")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /activate|approve|send/i })).not.toBeInTheDocument();
});
