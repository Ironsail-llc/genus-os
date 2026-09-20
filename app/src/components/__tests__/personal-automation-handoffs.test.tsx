import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { PersonalAutomationHandoffs } from "../personal-automation-handoffs";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("requests only a status check and never labels acknowledgment as completion", async () => {
  const fetcher=vi.fn<(url:string, options:RequestInit)=>Promise<{ok:boolean}>>(async()=>({ok:true}));vi.stubGlobal("fetch",fetcher);
  const refresh=vi.fn();
  render(<PersonalAutomationHandoffs handoffs={[{id:"handoff-1",operation_id:"op",kind:"push",state:"awaiting_external_action",origin:"https://shop.example",purpose:"Requested checkout",expires_at:"2030-01-01T00:00:00Z"}]} refresh={refresh} />);
  expect(screen.getByText("Device approval")).toBeTruthy();
  fireEvent.click(screen.getByRole("button",{name:"Check status after verification"}));
  expect(await screen.findByText("Status check requested. Completion still needs website confirmation.")).toBeTruthy();
  expect(fetcher.mock.calls[0][0]).toBe("/api/bridge/api/autonomy/handoffs/handoff-1/check");
  expect(fetcher.mock.calls[0][1].method).toBe("POST");
  expect(screen.queryByText("Completed")).toBeNull();
});
it("keeps expired verification uncertain and offers no resubmit button", () => {
  render(<PersonalAutomationHandoffs handoffs={[{id:"old",operation_id:"op",kind:"issuer",state:"expired",origin:"https://shop.example",purpose:"Requested checkout",expires_at:"2020-01-01T00:00:00Z"}]} refresh={vi.fn()} />);
  expect(screen.getByText("This verification handoff expired. The task still needs reconciliation.")).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull();
});
