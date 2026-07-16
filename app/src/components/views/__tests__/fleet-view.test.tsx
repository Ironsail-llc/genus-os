import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { FleetView } from "../fleet-view";

afterEach(() => vi.restoreAllMocks());

function mockFleet(rows: unknown[]) {
  vi.spyOn(global, "fetch").mockResolvedValue({
    ok: true, json: async () => rows,
  } as Response);
}

describe("FleetView", () => {
  it("renders agents and flags a capability without a constraint", async () => {
    mockFleet([
      { agent_id: "main", name: "Main", model: "m", sandbox: "host",
        tools_allowed: ["exec"], exec_allowlist: ["git status"], findings: [] },
      { agent_id: "loose", name: "Loose", model: "m", sandbox: "host",
        tools_allowed: ["exec"], exec_allowlist: [],
        findings: [{ code: "EXEC_NO_ALLOWLIST", message: "unconstrained shell" }] },
    ]);
    render(<FleetView visible />);
    const flagged = await screen.findByTestId("fleet-agent-loose");
    expect(flagged.getAttribute("data-finding")).toBe("true");
    expect(flagged.className).not.toMatch(/emerald|green/i);
    const clean = screen.getByTestId("fleet-agent-main");
    expect(clean.getAttribute("data-finding")).toBe("false");
  });

  it("does not fetch when hidden", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] } as Response);
    render(<FleetView visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("FleetView states", () => {
  it("shows a loading skeleton while fetching", () => {
    vi.spyOn(global, "fetch").mockReturnValue(new Promise(() => {}) as never);
    render(<FleetView visible />);
    expect(screen.getByTestId("fleet-loading")).toBeTruthy();
  });

  it("shows an empty state when there is nothing to list", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => [] } as Response);
    render(<FleetView visible />);
    expect(await screen.findByTestId("fleet-empty")).toBeTruthy();
  });
});
