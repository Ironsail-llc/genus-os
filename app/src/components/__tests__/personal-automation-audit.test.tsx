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
