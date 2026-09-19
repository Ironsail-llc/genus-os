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
