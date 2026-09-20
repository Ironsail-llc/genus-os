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

it("keeps renewal balances separate and clears them when closed", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ok:true,json:async()=>({event_count:3,reconciliation_required:true,
    position:{state:"charged",authorized_minor:0,charged_minor:100,refunded_minor:0,net_charged_minor:100},
    renewals:[{id:"opaque-renewal",due_on:"2026-02-28",schedule_matches:true,period_limit_exceeded:true,reconciliation_required:true,
      position:{state:"partially_refunded",authorized_minor:0,charged_minor:600,refunded_minor:200,net_charged_minor:400}}]})})));
  render(<PersonalAutomationPayment operationId="membership-1" currency="USD" />);
  fireEvent.click(screen.getByRole("button", {name:"Payment status"}));
  expect(await screen.findByText("Renewal due 2026-02-28")).toBeTruthy();
  expect(screen.getByText("Net charged: $1.00")).toBeTruthy();
  expect(screen.getByText("Net charged: $4.00")).toBeTruthy();
  expect(screen.getByText("Recorded charges exceed the recurring allowance for this billing period.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", {name:"Close payment status"}));
  expect(screen.queryByText("Renewal due 2026-02-28")).toBeNull();
});

it("separates an authorization release from refunded captured money", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ok:true,json:async()=>({event_count:4,reconciliation_required:false,
    position:{state:"partially_refunded",authorized_minor:600,authorization_open_minor:0,reversed_minor:200,charged_minor:400,refunded_minor:100,net_charged_minor:300}})})));
  render(<PersonalAutomationPayment operationId="order-1" currency="USD" />);
  fireEvent.click(screen.getByRole("button", {name:"Payment status"}));
  expect(await screen.findByText("Authorization released: $2.00")).toBeTruthy();
  expect(screen.getByText("Authorization remaining: $0.00")).toBeTruthy();
  expect(screen.getByText("Refunded: $1.00")).toBeTruthy();
  expect(screen.getByText("Net charged: $3.00")).toBeTruthy();
});
