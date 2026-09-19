import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LibraryPublication } from "../library-publication";

const packet = { kind: "knowledge", version: "claims-1", data: { claims: { access: "One connected pharmacy workflow." }, sources: [{ url: "https://example.com/services" }] } };
const preview = { packet, content_hash: "a".repeat(64) };
afterEach(() => vi.restoreAllMocks());
function load(data: unknown = packet) {
  fireEvent.change(screen.getByLabelText("Library review packet"), { target: { files: [new File([JSON.stringify(data)], "claims.json", { type: "application/json" })] } });
}
it("previews the packet and publishes exactly that content after explicit human review", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (url) => Response.json(String(url).endsWith("preview") ? preview : { ok: true }));
  const changed = vi.fn(); render(<LibraryPublication onChanged={changed} />); load();
  await screen.findByText("One connected pharmacy workflow.");
  expect(screen.queryByText(/Published by/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Publish reviewed version" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Publication review reason"), { target: { value: "Reviewed current business sources" } });
  fireEvent.click(screen.getByRole("checkbox", { name: "I reviewed the rules or claims and supporting sources" }));
  fireEvent.click(screen.getByRole("button", { name: "Publish reviewed version" }));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
  const writes = fetcher.mock.calls.filter(([url]) => String(url).endsWith("publication"));
  expect(writes).toHaveLength(1);
  expect(JSON.parse(String(writes[0][1]?.body))).toEqual({ packet, expected_hash: preview.content_hash, reason: "Reviewed current business sources" });
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("selection") || String(url).endsWith("settings"))).toBe(false);
});
it("checks the exact published record after an uncertain response without repeating publication", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (url) => {
    if (String(url).endsWith("publication")) throw new Error("Response lost");
    return Response.json(String(url).endsWith("preview") ? preview : { ...packet, content_hash: preview.content_hash });
  });
  render(<LibraryPublication onChanged={vi.fn()} />); load(); await screen.findByText("One connected pharmacy workflow.");
  fireEvent.change(screen.getByLabelText("Publication review reason"), { target: { value: "Reviewed current business sources" } });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Publish reviewed version" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/check/i);
  fireEvent.click(screen.getByRole("button", { name: "Check published version" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Publication confirmed");
  expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith("publication"))).toHaveLength(1);
});
it("invalidates review on file replacement and rejects oversized packets locally", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockResolvedValue(Response.json(preview));
  render(<LibraryPublication onChanged={vi.fn()} />); load(); await screen.findByText("One connected pharmacy workflow.");
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(screen.getByLabelText("Library review packet"), { target: { files: [new File(["x".repeat(131073)], "huge.json")] } });
  expect(await screen.findByRole("alert")).toHaveTextContent("128 KiB");
  expect(screen.queryByRole("button", { name: "Publish reviewed version" })).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(1);
});
