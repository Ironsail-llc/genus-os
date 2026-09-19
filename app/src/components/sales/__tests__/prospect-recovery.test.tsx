import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ProspectRecovery } from "../prospect-recovery";

afterEach(() => vi.restoreAllMocks());
it("resumes against the reviewed state and requires reload after uncertainty", async () => {
  const writes: unknown[] = [];
  vi.spyOn(global, "fetch").mockImplementation(async (_url, options) => {
    if (options?.method === "POST") { writes.push(JSON.parse(String(options.body))); return Response.json({ detail: "Reload required" }, { status: 409 }); }
    return Response.json({ state_hash: "a".repeat(64), prospect: { owner: "operator:test", status: "accepted" }, jobs: [], reservations: [] });
  });
  render(<ProspectRecovery prospectId="prospect-1" onChanged={async () => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Review preparation and ownership" }));
  await screen.findByText("operator:test");
  fireEvent.change(screen.getByLabelText("Recovery command"), { target: { value: "resume" } });
  fireEvent.change(screen.getByLabelText("Reason and preparation instructions"), { target: { value: "Return this reviewed conversation to Robothor" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply reviewed recovery" }));
  await screen.findByRole("alert");
  expect(writes).toEqual([{ command: "resume", expected_hash: "a".repeat(64), reason: "Return this reviewed conversation to Robothor" }]);
  await waitFor(() => expect(screen.getByRole("button", { name: "Apply reviewed recovery" })).toBeDisabled());
});
