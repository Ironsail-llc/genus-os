import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { PersonalAutomationPayment } from "../personal-automation-payment";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("does not describe a submitted checkout as a charge", async () => {
  const fetcher = vi.fn(async () => ({ok: true, json: async () => ({event_count: 1, reconciliation_required: false, position: {state: "submitted", authorized_minor: 0, charged_minor: 0, refunded_minor: 0, net_charged_minor: 0}})}));
  vi.stubGlobal("fetch", fetcher);
  render(<PersonalAutomationPayment operationId="order-1" currency="USD" />);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name: "Payment status"}));
  expect(await screen.findByText("Checkout submitted; charge not verified")).toBeTruthy();
  expect(screen.queryByText(/Charged:/)).toBeNull();
  fireEvent.click(screen.getByRole("button", {name: "Close payment status"}));
  expect(screen.queryByText("Checkout submitted; charge not verified")).toBeNull();
});
it("shows unresolved evidence without inventing a balance", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ok: true, json: async () => ({event_count: 1, reconciliation_required: true, position: null})})));
  render(<PersonalAutomationPayment operationId="order-1" currency="USD" />);
  fireEvent.click(screen.getByRole("button", {name: "Payment status"}));
  expect(await screen.findByText(/Payment evidence needs reconciliation/)).toBeTruthy();
  expect(screen.queryByText(/Charged:/)).toBeNull();
});

it("offers payment status for recurring subscription operations", async () => {
  const { PersonalAutomationPanel } = await import("../personal-automation-panel");
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ok: true, json: async () => url.endsWith("/status")
    ? {resources: [], grants: [], settings: {enabled:true, managed_browser:false, payment_processing:false}}
    : {operations:[{id:"membership-1",state:"completed",proposal:{action:"subscription",purpose:"Requested membership",origin:"https://club.example",currency:"USD"}}]}})));
  render(<PersonalAutomationPanel />);
  expect(await screen.findByRole("button", {name: "Payment status"})).toBeTruthy();
});
