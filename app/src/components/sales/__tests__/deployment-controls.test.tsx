import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DeploymentControls } from "../deployment-controls";

const API = "/api/sales/deployment";
const release = "a".repeat(64);
const transition = "00000000-0000-0000-0000-000000000001";
const target = { release_id: release, platform_revision: "b".repeat(40), agents: ["scout"], workflows: ["sales-work"], plugins: [{ name: "business-adapter", version: "1" }] };
const pending = { id: transition, direction: "deploy", status: "preparing", target_release_id: release, source_release_id: null,
  actor: "operator:human", reason: "Install reviewed fleet", target, source: { release_id: null, agents: [], workflows: [], plugins: [] } };
const empty = { configured: true, settings_revision: 4, selected_release_id: null, pending: null,
  history: [], rollback_candidate: null, control_busy: false, runtime: { ready: true, reason: null } };
const candidate = { ...target, name: "Example fleet", version: "candidate-1", source_revision: "c".repeat(40) };
afterEach(() => vi.restoreAllMocks());

it("requires inspection and reason before preparing, then commits only the pending transition", async () => {
  let state: unknown = empty;
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (url, options) => {
    if (options?.method === "POST") {
      state = String(url).endsWith("/prepare") ? { ...empty, pending } : { ...empty, selected_release_id: release, history: [{ ...pending, status: "committed" }] };
      return Response.json({ ...pending, status: String(url).endsWith("/prepare") ? "preparing" : "committed" });
    }
    return Response.json(String(url).includes("/releases/") ? candidate : state);
  });
  const changed = vi.fn();
  render(<DeploymentControls onChanged={changed} />);
  await screen.findByText("No fleet selected");
  const prepare = screen.getByRole("button", { name: "Prepare installation" });
  expect(prepare).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Release fingerprint"), { target: { value: release } });
  fireEvent.click(screen.getByRole("button", { name: "Inspect release" }));
  await screen.findByText("candidate-1");
  expect(prepare).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Reason for this change"), { target: { value: "Install reviewed fleet" } });
  fireEvent.click(prepare);
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith(API + "/prepare", expect.objectContaining({
    method: "POST", body: JSON.stringify({ release_id: release, expected_revision: 4, reason: "Install reviewed fleet" }),
  })));
  const commit = await screen.findByRole("button", { name: "Install reviewed release" });
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  fireEvent.click(commit);
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith(`${API}/transitions/${transition}/commit`, expect.objectContaining({ method: "POST", body: "{}" })));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(2));
  expect(fetcher.mock.calls.every(([url]) => !String(url).includes("settings"))).toBe(true);
});

it("invalidates an inspected release when the fingerprint changes", async () => {
  vi.spyOn(global, "fetch").mockImplementation(async (url) => Response.json(String(url).includes("releases") ? candidate : empty));
  render(<DeploymentControls onChanged={vi.fn()} />);
  await screen.findByText("No fleet selected");
  fireEvent.change(screen.getByLabelText("Release fingerprint"), { target: { value: release } });
  fireEvent.click(screen.getByRole("button", { name: "Inspect release" }));
  await screen.findByText("candidate-1");
  fireEvent.change(screen.getByLabelText("Reason for this change"), { target: { value: "Install reviewed fleet" } });
  fireEvent.change(screen.getByLabelText("Release fingerprint"), { target: { value: "d".repeat(64) } });
  expect(screen.getByRole("button", { name: "Prepare installation" })).toBeDisabled();
});

it("requires a status refresh after an uncertain response and never retries automatically", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (_url, options) => {
    if (options?.method === "POST") throw new Error("Connection lost");
    return Response.json({ ...empty, pending, runtime: { ready: false, reason: "Pending deployment" } });
  });
  render(<DeploymentControls onChanged={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Install reviewed release" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/refresh/i);
  expect(screen.getByRole("button", { name: "Install reviewed release" })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Refresh deployment status" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Install reviewed release" })).not.toBeDisabled());
});

it("shows recovery and holds mutations while another control operation runs", async () => {
  vi.spyOn(global, "fetch").mockResolvedValue(Response.json({ ...empty, pending, control_busy: true, runtime: { ready: false, reason: "Awaiting runtime reconciliation" } }));
  render(<DeploymentControls onChanged={vi.fn()} />);
  expect(await screen.findByText("Awaiting runtime reconciliation")).toBeVisible();
  expect(screen.getByRole("button", { name: "Install reviewed release" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Restore and cancel" })).toBeDisabled();
});

it("initializes the workspace with every integration explicitly paused", async () => {
  let configured = false;
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (_url, options) => {
    if (options?.method === "PATCH") { configured = true; return Response.json({}); }
    return Response.json({ ...empty, configured });
  });
  render(<DeploymentControls onChanged={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Initialize paused workspace" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/bridge/api/sales/settings", expect.objectContaining({
    method: "PATCH", body: JSON.stringify({ research_enabled: false, enrichment_enabled: false, promotion_enabled: false, sending_enabled: false, outcomes_enabled: false }),
  })));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Initialize paused workspace" })).not.toBeInTheDocument());
});
