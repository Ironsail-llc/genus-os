import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { PersonalAutomationAudit } from "../personal-automation-audit";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("fetches private text only on request and distinguishes unfetched links", async () => {
  const fetcher = vi.fn(async (url: string) => ({ok: true, json: async () => url.endsWith("/terms")
    ? {snapshots: [{id: "snapshot-1", version: 1, grant_version: 2, phase: "before_input", created_at: "2030-01-01"}]}
    : {snapshot: {coverage: "visible_text_only", omitted_frames: 1, documents: [{origin: "https://club.example", text: "<script>private text</script>", links: ["https://club.example/terms"], text_truncated: true, links_truncated: false}]}}}));
  vi.stubGlobal("fetch", fetcher);
  const {container} = render(<PersonalAutomationAudit operationId="operation-1"/>);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name: "Submission record"}));
  fireEvent.click(await screen.findByRole("button", {name: /View snapshot 1/}));
  expect(await screen.findByText("<script>private text</script>")).toBeTruthy();
  expect(container.querySelector("script")).toBeNull();
  expect(screen.getByText(/Contents of these links were not captured/)).toBeTruthy();
  expect(screen.getByText(/Text was shortened/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", {name: "Close submission record"}));
  expect(screen.queryByText("<script>private text</script>")).toBeNull();
});

it("identifies captured linked documents without calling their references uncaptured", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ok:true,json:async()=>url.endsWith("/terms")
    ? {snapshots:[{id:"snapshot-1",version:1,grant_version:1,phase:"before_input",created_at:"2030-01-01"}]}
    : {snapshot:{omitted_frames:0,documents:[
      {origin:"https://club.example",text:"Application",links:["https://club.example/terms"],source:"rendered"},
      {origin:"https://club.example",text:"Annual conditions",links:[],source:"linked_document",source_url:"https://club.example/terms-v2",requested_url:"https://club.example/terms"}
    ]}}})));
  render(<PersonalAutomationAudit operationId="operation-1"/>);
  fireEvent.click(screen.getByRole("button",{name:"Submission record"}));
  fireEvent.click(await screen.findByRole("button",{name:/View snapshot 1/}));
  expect(await screen.findByText("Annual conditions")).toBeTruthy();
  expect(screen.getByText(/Captured linked document/)).toBeTruthy();
  expect(screen.queryByText(/Contents of these links were not captured/)).toBeNull();
});

it("labels receipts and explains withheld text without rendering page contents", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url:string) => ({ok:true,json:async()=>url.endsWith("/terms")
    ? {snapshots:[{id:"receipt-1",version:3,grant_version:1,phase:"after_confirmation",created_at:"2030-01-01"}]}
    : {snapshot:{phase:"after_confirmation",capture_status:"withheld_after_code",omitted_frames:0,documents:[{origin:"https://shop.example",text:"Private field",links:[]}]}}})));
  render(<PersonalAutomationAudit operationId="operation-1" includeReceipts />);
  fireEvent.click(screen.getByRole("button",{name:"Submission and receipts"}));
  expect(await screen.findByText(/Receipt after confirmation/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button",{name:"View snapshot 3"}));
  expect(await screen.findByText("Receipt text was not saved because a verification code was used.")).toBeTruthy();
  expect(screen.queryByText("Private field")).toBeNull();
});


it("lets the owner erase the record, and the audit fact outlives it", async () => {
  // The archive kept the owner's name, date of birth, address and the answers
  // they typed into a website forever, with no owner-facing delete anywhere.
  let gone = false;
  const fetcher = vi.fn(async (url: string, init?: {method?: string}) => {
    if (init && init.method === "DELETE") { gone = true; return {ok: true, json: async () => ({erased: 1})}; }
    if (url.endsWith("/terms")) return {ok: true, json: async () => ({snapshots: [
      {id: "snapshot-1", version: 1, grant_version: 2, phase: "before_input", created_at: "2030-01-01", redacted_at: gone ? "2030-02-02" : null}]})};
    return {ok: true, json: async () => gone
      ? {snapshot: null, redacted_at: "2030-02-02"}
      : {snapshot: {omitted_frames: 0, documents: [{origin: "https://club.example", text: "private-canary-dob", links: []}]}}};
  });
  vi.stubGlobal("fetch", fetcher);
  render(<PersonalAutomationAudit operationId="operation-1"/>);
  fireEvent.click(screen.getByRole("button", {name: "Submission record"}));
  fireEvent.click(await screen.findByRole("button", {name: /View snapshot 1/}));
  expect(await screen.findByText("private-canary-dob")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", {name: /Erase submission record/}));
  expect(await screen.findByText(/Submission record erased/)).toBeTruthy();
  expect(screen.queryByText("private-canary-dob")).toBeNull();
  expect(fetcher).toHaveBeenCalledWith(
    "/api/bridge/api/autonomy/operations/operation-1/terms",
    expect.objectContaining({method: "DELETE"}),
  );
  // The audit fact survives: the snapshot is still listed.
  expect(await screen.findByText(/Grant version 2/)).toBeTruthy();
});

it("explains a suppressed pre-submit observation instead of an empty record", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url:string) => ({ok:true,json:async()=>url.endsWith("/terms")
    ? {snapshots:[{id:"snapshot-2",version:2,grant_version:1,phase:"before_submit",created_at:"2030-01-01"}]}
    : {snapshot:{phase:"before_submit",coverage:"suppressed_after_code",omitted_frames:0,documents:[{origin:"https://shop.example",text:"",links:[]}]}}})));
  const {container} = render(<PersonalAutomationAudit operationId="operation-1"/>);
  fireEvent.click(screen.getByRole("button",{name:"Submission record"}));
  fireEvent.click(await screen.findByRole("button",{name:"View snapshot 2"}));
  expect(await screen.findByText(/Page text was not saved for this step/)).toBeTruthy();
  expect(container.querySelector("pre")).toBeNull();

});
