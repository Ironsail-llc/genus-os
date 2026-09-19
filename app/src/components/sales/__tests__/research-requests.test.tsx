import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ResearchRequests } from "../research-requests";

afterEach(() => vi.restoreAllMocks());
const request = { id: "request-1", revision: 2, status: "active", config: { title: "Florida clinics", query: "US prescribing clinics in Florida", target_companies: 10, buying_case: "network_access" } };

it("creates a bounded request without enabling sending and preserves its key after an uncertain response", async () => {
  const writes: Record<string, unknown>[] = [];
  vi.spyOn(global, "fetch").mockImplementation(async (_url, options) => {
    if (options?.method === "POST") { writes.push(JSON.parse(String(options.body))); return Response.json({ detail: "Connection interrupted" }, { status: 503 }); }
    return Response.json({ items: [], next_cursor: null });
  });
  render(<ResearchRequests buyingCases={["network_access"]} />);
  await screen.findByText("No research requests yet.");
  fireEvent.change(screen.getByLabelText("Request title"), { target: { value: "Florida clinics" } });
  fireEvent.change(screen.getByLabelText("Businesses to find"), { target: { value: "US prescribing clinics in Florida" } });
  fireEvent.change(screen.getByLabelText("New companies to research"), { target: { value: "10" } });
  fireEvent.click(screen.getByRole("button", { name: "Create research request" }));
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Retry the same request" }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[0]).toEqual(writes[1]);
  expect(writes[0]).toMatchObject({ title: "Florida clinics", target_companies: 10, buying_case: "network_access" });
  expect(writes[0]).not.toHaveProperty("sending_enabled");
});

it("shows progress and pauses using the reviewed revision and reason", async () => {
  const writes: unknown[] = [];
  vi.spyOn(global, "fetch").mockImplementation(async (url, options) => {
    if (options?.method === "POST") { writes.push(JSON.parse(String(options.body))); return Response.json({ ...request, status: "paused", revision: 3, progress: { phase: "paused", discovered: 4, assessed: 2, remaining: 6, spent_units: 100000, reserved_units: 200000, failed_jobs: 0 }, jobs: [] }); }
    if (String(url).endsWith("/request-1")) return Response.json({ ...request, progress: { phase: "discovering", discovered: 4, assessed: 2, remaining: 6, spent_units: 100000, reserved_units: 200000, failed_jobs: 0 }, jobs: [] });
    return Response.json({ items: [request], next_cursor: null });
  });
  render(<ResearchRequests buyingCases={["network_access"]} />);
  fireEvent.click(await screen.findByRole("button", { name: /Florida clinics/ }));
  expect(await screen.findByText(/4 discovered/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Reason for request change"), { target: { value: "Pause for operator review" } });
  fireEvent.click(screen.getByRole("button", { name: "Pause this request" }));
  await waitFor(() => expect(writes).toEqual([{ status: "paused", expected_revision: 2, reason: "Pause for operator review" }]));
});
