import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ReadRecovery } from "@/components/sales/read-recovery";

afterEach(() => vi.restoreAllMocks());
const job = { id: "read-1", kind: "sales.business", status: "failed", error: "Business provider page requires review",
  attempts: 5, max_attempts: 5, updated_at: "2026-09-18T12:00:00Z", scope: { source: "orders_app", account_id: "account-1", kind: "practice" } };

it("retries only the selected inactive read and records the operator's repair reason", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ items: [job], next_cursor: null }) } as Response);
  render(<ReadRecovery />);
  fireEvent.click(await screen.findByRole("button", { name: "Review read recovery" }));
  const retry = screen.getByRole("button", { name: "Retry this read" });
  expect(retry).toBeDisabled();
  fireEvent.change(screen.getByLabelText("What was repaired?"), { target: { value: "Verified and repaired the scoped source account" } });
  fireEvent.click(retry);
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/bridge/api/sales/jobs/read-1/retry", expect.objectContaining({
    method: "POST", body: JSON.stringify({ reason: "Verified and repaired the scoped source account" }) })));
  expect(await screen.findByRole("status")).toHaveTextContent("Read queued for retry");
});

it("shows active reads without allowing recovery to replace their lease", async () => {
  vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ items: [{ ...job, status: "running" }], next_cursor: null }) } as Response);
  render(<ReadRecovery />);
  expect(await screen.findByText("running")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Review read recovery" })).not.toBeInTheDocument();
});

it("invalidates a retry when the job has already changed", async () => {
  vi.spyOn(global, "fetch").mockImplementation(async (_path, options) => options?.method === "POST"
    ? { ok: false, status: 409, statusText: "Conflict" } as Response
    : { ok: true, json: async () => ({ items: [job], next_cursor: null }) } as Response);
  render(<ReadRecovery />);
  fireEvent.click(await screen.findByRole("button", { name: "Review read recovery" }));
  fireEvent.change(screen.getByLabelText("What was repaired?"), { target: { value: "Repaired source account configuration" } });
  fireEvent.click(screen.getByRole("button", { name: "Retry this read" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/already running/);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Retry this read" })).not.toBeInTheDocument();
});
