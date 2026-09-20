import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PersonalAutomationPanel } from "@/components/personal-automation-panel";

const fetchMock = vi.fn();
beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string) => ({ ok: true, json: async () =>
    url.endsWith("/status") ? { resources: [], grants: [], settings: {
      enabled: false, managed_browser: false, payment_processing: false, payment_assessment_reference: "" } } :
    url.endsWith("/operations") ? { operations: [] } : { id: "reference" } }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); fetchMock.mockReset(); });

describe("personal automation setup", () => {
  it("imports contact information without placing values into the page or request", async () => {
    render(<PersonalAutomationPanel />);
    const button = await screen.findByRole("button", { name: "Use my saved contact details" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/bridge/api/autonomy/profile-from-contact", expect.objectContaining({ method: "POST", body: "{}" })));
  });
  it("requires an explicit choice for general website authority and preserves spending caps", async () => {
    render(<PersonalAutomationPanel />);
    const choice = await screen.findByLabelText("Allow any public HTTPS website");
    expect(choice).not.toBeChecked();
    fireEvent.click(choice);
    fireEvent.change(screen.getByLabelText("Authority expires"), { target: { value: "2030-01-01" } });
    fireEvent.change(screen.getByLabelText("Per purchase (USD)"), { target: { value: "25" } });
    fireEvent.change(screen.getByLabelText("Monthly total (USD)"), { target: { value: "100" } });
    const grant = screen.getByRole("button", { name: "Grant authority" });
    await waitFor(() => expect(grant).toBeEnabled());
    fireEvent.submit(grant.closest("form")!);
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => url.endsWith("/grants"));
      expect(call).toBeDefined();
      expect(JSON.parse(call![1].body)).toMatchObject({ allow_any_website: true,
        origins: [], per_purchase_minor: 2500, monthly_minor: 10000 });
    });
  });
});

it("shows the saved limits and separates projected renewal charges from settled payments", async () => {
  fetchMock.mockImplementation(async (url: string) => ({ ok: true, json: async () =>
    url.endsWith("/status") ? { resources: [], grants: [{ id: "grant", revoked: false, policy: {
      origins: [], allow_any_website: true, currency: "USD", per_purchase_minor: 100000,
      monthly_minor: 100000, recurring_minor: 100000, annual_minor: 1200000 } }],
      spending: { state: "ready", months: { USD: { "2026-09": 0, "2026-10": 60000 } } },
      settings: { enabled: true, managed_browser: false, payment_processing: false,
        payment_assessment_reference: "" } } : { operations: [] } }));
  render(<PersonalAutomationPanel />);
  expect(await screen.findByText("Per purchase: $1,000.00 · Monthly total: $1,000.00")).toBeInTheDocument();
  expect(screen.getByText("Per recurring charge: $1,000.00 · Annual commitment: $12,000.00")).toBeInTheDocument();
  expect(screen.getByText("Projected charges")).toBeInTheDocument();
  expect(screen.getByText("2026-10")).toBeInTheDocument();
  expect(screen.getByText("$600.00")).toBeInTheDocument();
});

it("says plainly that revoking does not cancel a scheduled renewal", async () => {
  fetchMock.mockImplementation(async (url: string) => ({ ok: true, json: async () =>
    url.endsWith("/status") ? { resources: [], grants: [{ id: "grant", revoked: false, policy: {
      origins: [], allow_any_website: true, currency: "USD", per_purchase_minor: 100000,
      monthly_minor: 100000, recurring_minor: 100000, annual_minor: 1200000 } }],
      settings: { enabled: true, managed_browser: false, payment_processing: false,
        payment_assessment_reference: "" } } : { operations: [] } }));
  render(<PersonalAutomationPanel />);
  expect(await screen.findByText(/does not cancel a scheduled renewal/)).toBeInTheDocument();
});

it("shows a frozen grant and lets the owner clear the payment hold", async () => {
  fetchMock.mockImplementation(async (url: string) => ({ ok: true, json: async () =>
    url.endsWith("/status") ? { resources: [], grants: [{ id: "grant", revoked: false,
      payment_hold: true, policy: {
      origins: [], allow_any_website: true, currency: "USD", per_purchase_minor: 100000,
      monthly_minor: 100000, recurring_minor: 100000, annual_minor: 1200000 } }],
      settings: { enabled: true, managed_browser: false, payment_processing: false,
        payment_assessment_reference: "" } } : { operations: [] } }));
  render(<PersonalAutomationPanel />);
  expect(await screen.findByText(/Spending paused/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Clear payment hold" }));
  await waitFor(() => {
    const call = fetchMock.mock.calls.find(([url]) => url.endsWith("/grants/grant/payment-hold"));
    expect(call).toBeDefined();
    expect(call![1].method).toBe("DELETE");
  });
});

it("shows saved field names and provenance and can check older enrollments", async () => {
  fetchMock.mockImplementation(async (url: string) => ({ ok: true, json: async () =>
    url.endsWith("/status") ? { resources: [
      {id:"profile",kind:"profile",label:"Contact",descriptor:{version:1,fields:["email","first_name"],source:"linked_contact"}},
      {id:"older",kind:"document",label:"Photo"}], grants: [], settings: {
      enabled:true,managed_browser:false,payment_processing:false,payment_assessment_reference:""} } :
    url.endsWith("/operations") ? {operations:[]} : {updated:1} }));
  render(<PersonalAutomationPanel />);
  expect(await screen.findByText("Saved fields: email, first name")).toBeInTheDocument();
  expect(screen.getByText("Imported from your linked contact")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"Check saved information"}));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/bridge/api/autonomy/resources/refresh-descriptions",expect.objectContaining({method:"POST",body:"{}"})));
});
