import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { IntegrationSetup } from "../integration-setup";
afterEach(() => vi.restoreAllMocks());
it("reviews setup changes without allowing credentials or integration switches", async () => {
  const writes: unknown[] = [];
  const snapshot = { revision: 3, config: { senders: [], mailbox_approved_until: {}, business_sources: [], discovery_segments: [], postal_address: "", unsubscribe_url: "" }, credentials: [{ provider: "instantly", complete: false, keys: [{ name: "providers/instantly/api_key", present: false }] }], business_services: [], provider_checks: [], readiness: {}, notes: [] };
  vi.spyOn(global, "fetch").mockImplementation(async (_url, options) => {
    if (options?.method === "POST") { writes.push(JSON.parse(String(options.body))); return Response.json({ ...snapshot, revision: 4 }); }
    return Response.json(snapshot);
  });
  render(<IntegrationSetup onChanged={async () => {}} />);
  await screen.findByText("providers/instantly/api_key");
  fireEvent.change(screen.getByLabelText("Business postal address"), { target: { value: "TEST POSTAL ADDRESS" } });
  fireEvent.change(screen.getByLabelText("Setup review reason"), { target: { value: "Reviewed the business sender identity" } });
  fireEvent.click(screen.getByRole("button", { name: "Review setup changes" }));
  expect(writes).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Save reviewed setup" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toEqual({ expected_revision: 3, reason: "Reviewed the business sender identity", changes: { postal_address: "TEST POSTAL ADDRESS" } });
  expect(screen.queryByLabelText(/API token|API key value/i)).not.toBeInTheDocument();
});
