import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PersonalAutomationPanel } from "../personal-automation-panel";

const token = "x".repeat(43);
let calls: { url: string; body: Record<string, unknown> }[];
beforeEach(() => {
  calls = [];
  window.history.replaceState({}, "", "/account/autonomy#enroll=" + token);
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: JSON.parse(String(init?.body || "{}")) });
    const body = url.endsWith("/inspect") ? { kind: "credential", origin: "https://shop.example", resource_id: null }
      : url.endsWith("/status") ? { resources: [], grants: [], settings: { enabled: true } }
      : url.endsWith("/operations") ? { operations: [] } : { id: "saved-reference" };
    return { ok: true, json: async () => body };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState({}, "", "/"); });

describe("scoped private enrollment", () => {
  it("removes the URL token, binds the website and submits privately without a chat request", async () => {
    render(<PersonalAutomationPanel />);
    await screen.findByLabelText("Password");
    expect(window.location.hash).toBe("");
    expect((screen.getByLabelText("Information type") as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByLabelText("Website") as HTMLInputElement).value).toBe("https://shop.example");
    expect((screen.getByLabelText("Website") as HTMLInputElement).readOnly).toBe(true);
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Website login" } });
    fireEvent.change(screen.getByLabelText("Username or email"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "private-ui-canary" } });
    fireEvent.click(screen.getByText("Save information"));
    await waitFor(() => expect(calls.some(c => c.url.endsWith("/complete"))).toBe(true));
    const call = calls.find(c => c.url.endsWith("/complete"))!;
    expect(call.body.token).toBe(token);
    expect(JSON.stringify(call.body.resource)).toContain("private-ui-canary");
    expect(calls.every(c => c.url.startsWith("/api/bridge/api/autonomy/"))).toBe(true);
    await waitFor(() => expect(screen.queryByDisplayValue("private-ui-canary")).toBeNull());
    expect(screen.queryByText(/private-ui-canary/)).toBeNull();
  });

  it("handles an enrollment link opened on the current page and clears fields for the next link", async () => {
    window.history.replaceState({}, "", "/account/autonomy");
    render(<PersonalAutomationPanel />);
    await waitFor(() => expect((screen.getByLabelText("Information type") as HTMLSelectElement).disabled).toBe(false));
    window.location.hash = "enroll=" + token;
    await screen.findByLabelText("Password");
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "previous-website-secret" } });
    window.location.hash = "enroll=" + "y".repeat(43);
    await waitFor(() => expect(calls.some(c => c.body.token === "y".repeat(43))).toBe(true));
    await waitFor(() => expect(screen.queryByDisplayValue("previous-website-secret")).toBeNull());
    expect(window.location.hash).toBe("");
  });

  it("keeps enrollment unavailable when its token is rejected", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, json: async () => ({ detail: "Enrollment expired" }) })));
    render(<PersonalAutomationPanel />);
    await screen.findByText("Enrollment expired");
    expect((screen.getByText("Save information") as HTMLButtonElement).disabled).toBe(true);
    expect(window.location.hash).toBe("");
  });
});
