import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
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

describe("FleetView payload", () => {
  it("shows the empty state, not a crash, when a 200 carries something other than a list", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ messages: [], active: false }),
    } as Response);
    render(<FleetView visible />);
    expect(await screen.findByTestId("fleet-empty")).toBeTruthy();
  });
});

describe("FleetView unreadable reply", () => {
  it("says the bridge could not be read when a 200 body fails to parse", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => { throw new SyntaxError("Unexpected token <"); },
    } as unknown as Response);
    render(<FleetView visible />);
    expect(await screen.findByText("Could not reach the bridge.")).toBeTruthy();
  });
});

describe("FleetView cache", () => {
  it("paints from cache without a request when shown again within a minute", async () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => [{ agent_id: "main", name: "Main", findings: [] }],
    } as Response);
    const client = new QueryClient();
    const at = (visible: boolean) => (
      <QueryClientProvider client={client}>
        <FleetView visible={visible} />
      </QueryClientProvider>
    );
    const { rerender } = render(at(true));
    expect(await screen.findByTestId("fleet-agent-main")).toBeTruthy();
    expect(spy).toHaveBeenCalledTimes(1);

    rerender(at(false));
    rerender(at(true));
    await waitFor(() => expect(screen.getByTestId("fleet-agent-main")).toBeTruthy());
    expect(spy).toHaveBeenCalledTimes(1);
  });
});
