import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SalesView } from "../sales-view";

afterEach(() => vi.restoreAllMocks());
const data = { prospects: [], jobs: [], budgets: [], settings: { sending_enabled: false },
  actions: [{ id: "draft-1", status: "review", expires_at: "2099-01-01T00:00:00Z", payload: {
    sender: "sales@example.com", recipient: "alice@example.com", subject: "A question",
    body: "Exact approved message", claim_ids: ["access"], evidence_ids: ["service"],
    knowledge_version: "v1", purpose: "initial", prospect_id: "company-1" } }] };

describe("Sales review", () => {
  it("distinguishes a scheduled campaign from confirmed delivery", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ ...data,
      actions: [{ ...data.actions[0], status: "completed", receipt: { id: "campaign-1", delivery_status: "scheduled" } }] }) } as Response);
    render(<SalesView visible role="admin" />);
    expect(await screen.findByText(/scheduled/)).toBeTruthy();
  });
  it("shows exact content and approves only the selected draft", async () => {
    const fetcher = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => data } as Response);
    render(<SalesView visible role="admin" />);
    expect(await screen.findByText("Exact approved message")).toBeTruthy();
    expect(screen.getByText(/alice@example.com/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve this message" }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith(
      "/api/bridge/api/sales/actions/draft-1/decision", expect.objectContaining({
        method: "POST", body: JSON.stringify({ approved: true }) })));
  });
  it("does not load tenant sales data for a member or a hidden view", () => {
    const fetcher = vi.spyOn(global, "fetch");
    const { rerender } = render(<SalesView visible role="member" />);
    expect(screen.getByText(/operator access required/i)).toBeTruthy();
    rerender(<SalesView visible={false} role="admin" />);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("reports a failed approval without pretending it succeeded", async () => {
    vi.spyOn(global, "fetch").mockImplementation(async (_url, init) => {
      if (init?.method === "POST") return { ok: false, status: 409, statusText: "Stale approval" } as Response;
      return { ok: true, json: async () => data } as Response;
    });
    render(<SalesView visible role="admin" />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve this message" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("409");
    expect(screen.getByText("Exact approved message")).toBeTruthy();
  });
});

it("loads deployment state only when the operator opens its controls", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (url) => Response.json(String(url) === "/api/sales/deployment" ? {
    configured: true, selected_release_id: null, settings_revision: 1, pending: null, history: [], rollback_candidate: null,
    control_busy: false, runtime: { ready: true, reason: null },
  } : data));
  render(<SalesView visible role="admin" />);
  await screen.findByText("Exact approved message");
  expect(fetcher.mock.calls.every(([url]) => String(url) !== "/api/sales/deployment")).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Manage sales deployment" }));
  expect(await screen.findByText("No fleet selected")).toBeVisible();
});


it("loads pilot settings only when the operator opens the editor", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (url) => Response.json(String(url).endsWith("/settings")
    ? { config: {}, revision: 0 } : data));
  render(<SalesView visible role="admin" />);
  await screen.findByText("Exact approved message");
  expect(fetcher.mock.calls.every(([url]) => !String(url).endsWith("/settings"))).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Review pilot settings" }));
  expect(await screen.findByLabelText("Monthly spending limit (USD)")).toHaveValue("0");
});


it("loads the published sales library only when its review panel opens", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (url) => Response.json(String(url).includes("/library?")
    ? { items: [], next_cursor: null } : String(url).endsWith("/settings") ? { config: {}, revision: 0 } : data));
  render(<SalesView visible role="admin" />);
  await screen.findByText("Exact approved message");
  expect(fetcher.mock.calls.every(([url]) => !String(url).includes("/library?"))).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Review sales library" }));
  expect(await screen.findByText("No qualification policies have been published yet.")).toBeVisible();
});
