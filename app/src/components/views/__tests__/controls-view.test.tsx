import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import ControlsView from "../controls-view";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ControlsView", () => {
  it("renders an INERT flag as a warning, never as healthy/green", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ([{
        name: "ROBOTHOR_APPROVAL_MODE", value: "enforce",
        verdict: { status: "INERT", message: "NEVER FIRED — this control cannot protect you.",
                   last_fired: null, count_7d: 0 },
      }]),
    } as Response);

    render(<ControlsView />);
    expect(await screen.findByText(/NEVER FIRED/i)).toBeInTheDocument();
    const badge = await screen.findByTestId("verdict-ROBOTHOR_APPROVAL_MODE");
    expect(badge).toHaveAttribute("data-status", "INERT");
    expect(badge.className).not.toMatch(/green|ok|healthy/i);
  });

  it("renders an ENFORCING flag affirmatively", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ([{
        name: "ROBOTHOR_RBAC_MODE", value: "enforce",
        verdict: { status: "ENFORCING", message: "last fired 2026-07-14 09:00 (12 events / 7d)",
                   last_fired: "2026-07-14T09:00:00Z", count_7d: 12 },
      }]),
    } as Response);

    render(<ControlsView />);
    const badge = await screen.findByTestId("verdict-ROBOTHOR_RBAC_MODE");
    expect(badge).toHaveAttribute("data-status", "ENFORCING");
    expect(badge.className).toMatch(/emerald/i);
  });

  it("requires a reason before allowing a mode change to be applied", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ([{
        name: "ROBOTHOR_JUDGE_ENABLED", value: "false",
        verdict: { status: "UNPROVEN", message: "disabled", last_fired: null, count_7d: 0 },
      }]),
    } as Response);

    render(<ControlsView />);
    const saveButton = await screen.findByTestId("save-ROBOTHOR_JUDGE_ENABLED");
    expect(saveButton).toBeDisabled();
  });
});
