import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { GmailRecovery } from "../gmail-recovery";

afterEach(() => vi.restoreAllMocks());
const proof = { content_hash: "a".repeat(64), receipt: { id: "gmail:scope:123", delivery_status: "sent_copy_verified" },
  message: { sender: "sales@example.com", recipient: "alice@example.com", subject: "Approved subject", body: "Approved body", occurred_at: "2026-09-19T12:00:00Z" } };

it("inspects the sent copy and then submits only its exact review hash and reason", async () => {
  const calls: { url: string; body: unknown }[] = [];
  const changed = vi.fn(async () => {});
  vi.spyOn(global, "fetch").mockImplementation(async (url, options) => {
    calls.push({ url: String(url), body: options?.body ? JSON.parse(String(options.body)) : null });
    return Response.json(String(url).endsWith("/inspect") ? proof : proof.receipt);
  });
  render(<GmailRecovery actionId="action-1" onChanged={changed} />);
  expect(screen.queryByRole("button", { name: "Reconcile this sent message" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Inspect Gmail sent copy" }));
  await screen.findByText("Approved body");
  expect(calls).toHaveLength(1);
  fireEvent.change(screen.getByLabelText("Gmail recovery review reason"), { target: { value: "Reviewed the exact recipient and sent copy" } });
  fireEvent.click(screen.getByRole("button", { name: "Reconcile this sent message" }));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
  expect(calls[1].body).toEqual({ expected_hash: proof.content_hash, reason: "Reviewed the exact recipient and sent copy" });
  expect(calls.every(c => !c.url.endsWith("/send"))).toBe(true);
});

it("discards a stale proof and requires another inspection after conflict", async () => {
  vi.spyOn(global, "fetch").mockImplementation(async (url) => String(url).endsWith("/inspect")
    ? Response.json(proof) : Response.json({ detail: "Evidence changed" }, { status: 409 }));
  render(<GmailRecovery actionId="action-2" onChanged={async () => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect Gmail sent copy" }));
  await screen.findByText("Approved body");
  fireEvent.change(screen.getByLabelText("Gmail recovery review reason"), { target: { value: "Reviewed the exact recipient and sent copy" } });
  fireEvent.click(screen.getByRole("button", { name: "Reconcile this sent message" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Reconcile this sent message" })).toBeNull();
});
