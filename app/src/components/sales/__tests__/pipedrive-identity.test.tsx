import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PipedriveIdentity } from "../pipedrive-identity";
afterEach(() => vi.restoreAllMocks());
it("fetches canonical identities before allowing exact packet adoption", async () => {
  const writes: { url: string; body: unknown }[] = [];
  vi.spyOn(global, "fetch").mockImplementation(async (url, options) => {
    writes.push({ url: String(url), body: options?.body ? JSON.parse(String(options.body)) : null });
    if (String(url).endsWith("/inspect")) return Response.json({ id: "packet-1", content_hash: "a".repeat(64), content: { records: { organization: { id: 11, name: "Example Clinic" }, people: [], lead: null }, observed_at: "2026-09-19T12:00:00Z" } });
    return Response.json({});
  });
  render(<PipedriveIdentity prospectId="prospect-1" onChanged={async () => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Review existing Pipedrive identities" }));
  fireEvent.change(screen.getByLabelText("Organization ID"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: "Fetch selected records" }));
  await screen.findByText("Example Clinic");
  fireEvent.change(screen.getByLabelText("Identity review reason"), { target: { value: "Confirmed this is the same prescribing practice" } });
  fireEvent.click(screen.getByRole("button", { name: "Adopt reviewed identities" }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1].body).toEqual({ packet_id: "packet-1", expected_hash: "a".repeat(64), reason: "Confirmed this is the same prescribing practice" });
  expect(writes[1].body).not.toHaveProperty("organization_id");
});
