import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { QualificationAssessment } from "../qualification-assessment";

it("shows independent reasoning alongside references to original source passages", () => {
  render(<QualificationAssessment assessment={{ criteria: { prescribing: { status: "unknown", evidence_ids: ["menu"], explanation: "A service menu does not establish prescribing." } }, research_gaps: ["Find explicit prescription care."] }} evidence={[{ id: "menu", url: "https://clinic.example.com/services", excerpt: "Wellness consultations" }]} />);
  expect(screen.getByText("A service menu does not establish prescribing.")).toBeTruthy();
  expect(screen.getByText("Wellness consultations")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Source menu" })).toHaveAttribute("href", "https://clinic.example.com/services");
  expect(screen.getByText(/Find explicit prescription care/)).toBeTruthy();
});
