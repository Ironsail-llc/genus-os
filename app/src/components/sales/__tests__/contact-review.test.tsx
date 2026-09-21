import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ContactReview } from "../contact-review";
afterEach(() => vi.restoreAllMocks());
it("shows contact source and reviews identity without overriding deliverability", async () => {
  const writes: unknown[] = [];
  const contact = { id: "contact-1", identity_hash: "a".repeat(64), data: { name: "Alice", role: "Owner", email: "alice@example.com", source_url: "https://clinic.example.com/team", verification: "valid" }, review: { current: false } };
  vi.spyOn(global, "fetch").mockImplementation(async (_url, options) => {
    if (options?.method === "POST") { writes.push(JSON.parse(String(options.body))); return Response.json(contact); }
    return Response.json([contact]);
  });
  render(<ContactReview prospectId="prospect-1" onChanged={async () => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Review business contacts" }));
  expect(await screen.findByRole("link", { name: "Public contact source" })).toHaveAttribute("href", contact.data.source_url);
  fireEvent.click(screen.getByRole("button", { name: "Review this identity" }));
  fireEvent.change(screen.getByLabelText("Contact name"), { target: { value: "Practice team" } });
  fireEvent.change(screen.getByLabelText("Contact review reason"), { target: { value: "This is a shared practice inbox" } });
  fireEvent.click(screen.getByRole("button", { name: "Save reviewed identity" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toMatchObject({ name: "Practice team", expected_hash: contact.identity_hash });
  expect(writes[0]).not.toHaveProperty("verification");
});
