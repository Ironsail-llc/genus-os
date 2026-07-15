import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { useSession } from "next-auth/react";
import ControlsView from "../controls-view";

vi.mock("next-auth/react", () => ({
  useSession: vi.fn(),
}));

const mockUseSession = vi.mocked(useSession);

function mockSessionRole(role: string | undefined) {
  mockUseSession.mockReturnValue({
    data: role
      ? ({ role, user: { role } } as unknown as ReturnType<typeof useSession>["data"])
      : null,
    status: role ? "authenticated" : "unauthenticated",
    update: vi.fn(),
  } as ReturnType<typeof useSession>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ControlsView", () => {
  it("renders an INERT flag as a warning, never as healthy/green", async () => {
    mockSessionRole("owner");
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ([{
        name: "ROBOTHOR_APPROVAL_MODE", value: "enforce",
        valid_values: ["off", "observe", "alert", "enforce"],
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
    mockSessionRole("owner");
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ([{
        name: "ROBOTHOR_RBAC_MODE", value: "enforce",
        valid_values: ["off", "observe", "alert", "enforce"],
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
    mockSessionRole("owner");
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ([{
        name: "ROBOTHOR_JUDGE_ENABLED", value: "false",
        valid_values: ["true", "false"],
        verdict: { status: "UNPROVEN", message: "disabled", last_fired: null, count_7d: 0 },
      }]),
    } as Response);

    render(<ControlsView />);
    const saveButton = await screen.findByTestId("save-ROBOTHOR_JUDGE_ENABLED");
    expect(saveButton).toBeDisabled();
  });

  it("hides the write control for a non-operator role (viewer)", async () => {
    mockSessionRole("viewer");
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ([{
        name: "ROBOTHOR_RBAC_MODE", value: "enforce",
        valid_values: ["off", "observe", "alert", "enforce"],
        verdict: { status: "ENFORCING", message: "last fired 2026-07-14 09:00 (12 events / 7d)",
                   last_fired: "2026-07-14T09:00:00Z", count_7d: 12 },
      }]),
    } as Response);

    render(<ControlsView />);
    await screen.findByTestId("control-ROBOTHOR_RBAC_MODE");
    expect(screen.queryByTestId("select-ROBOTHOR_RBAC_MODE")).not.toBeInTheDocument();
    expect(screen.queryByTestId("reason-ROBOTHOR_RBAC_MODE")).not.toBeInTheDocument();
    expect(screen.queryByTestId("save-ROBOTHOR_RBAC_MODE")).not.toBeInTheDocument();
    expect(screen.getByTestId("readonly-note-ROBOTHOR_RBAC_MODE")).toHaveTextContent(
      /operator only/i
    );
    // Reading the mode/verdict is still visible for a non-operator.
    expect(screen.getByText(/mode: enforce/i)).toBeInTheDocument();
  });

  it("shows the write control for an operator role (owner)", async () => {
    mockSessionRole("owner");
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ([{
        name: "ROBOTHOR_RBAC_MODE", value: "enforce",
        valid_values: ["off", "observe", "alert", "enforce"],
        verdict: { status: "ENFORCING", message: "last fired 2026-07-14 09:00 (12 events / 7d)",
                   last_fired: "2026-07-14T09:00:00Z", count_7d: 12 },
      }]),
    } as Response);

    render(<ControlsView />);
    expect(await screen.findByTestId("select-ROBOTHOR_RBAC_MODE")).toBeInTheDocument();
    expect(screen.getByTestId("reason-ROBOTHOR_RBAC_MODE")).toBeInTheDocument();
    expect(screen.getByTestId("save-ROBOTHOR_RBAC_MODE")).toBeInTheDocument();
    expect(screen.queryByTestId("readonly-note-ROBOTHOR_RBAC_MODE")).not.toBeInTheDocument();
  });
});
