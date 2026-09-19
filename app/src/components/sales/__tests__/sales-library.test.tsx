import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SalesLibrary } from "../sales-library";

const policy = { kind: "qualification", version: "v1", approved_by: "operator:reviewer", approved_at: "2026-09-19T00:00:00Z",
  data: { buying_case: "network_access", required: ["prescribing"], weights: { prescribing: 100 }, threshold: 80, max_evidence_age_days: 90 } };
const knowledge = { ...policy, kind: "knowledge", data: { claims: { access: "Access participating pharmacies." } } };
const empty = { revision: 4, config: { active_policy_versions: {}, active_knowledge_version: "", sending_enabled: false } };
afterEach(() => vi.restoreAllMocks());
function backend(fail = false) {
  return vi.spyOn(global, "fetch").mockImplementation(async (url, options) => {
    if (options?.method === "POST") return fail ? Response.json({}, { status: 409 }) : Response.json({ revision: 5,
      config: { ...empty.config, active_policy_versions: { network_access: "v1" }, active_knowledge_version: "v1" } });
    if (String(url).includes("kind=qualification")) return Response.json({ items: [policy], next_cursor: null });
    if (String(url).includes("kind=knowledge")) return Response.json({ items: [knowledge], next_cursor: null });
    return Response.json(empty);
  });
}
async function select() {
  fireEvent.change(await screen.findByLabelText("Qualification for network access"), { target: { value: "v1" } });
  fireEvent.change(screen.getByLabelText("Active claim library"), { target: { value: "v1" } });
  fireEvent.change(screen.getByLabelText("Reason for library selection"), { target: { value: "Reviewed pilot rules and claims" } });
  fireEvent.click(screen.getByRole("button", { name: "Review library selection" }));
}
it("shows exact published rules and claims before selecting without enabling sending", async () => {
  const fetcher = backend();
  const changed = vi.fn();
  render(<SalesLibrary onChanged={changed} />);
  await select();
  const preview = screen.getByRole("region", { name: "Review selected library" });
  expect(preview).toHaveTextContent("Access participating pharmacies.");
  expect(preview).toHaveTextContent("prescribing: 100 points · required");
  expect(preview).toHaveTextContent("operator:reviewer");
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Use reviewed library" }));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
  const writes = fetcher.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(writes).toHaveLength(1);
  expect(JSON.parse(String(writes[0][1]?.body))).toEqual({ policy_versions: { network_access: "v1" }, knowledge_version: "v1", expected_revision: 4, reason: "Reviewed pilot rules and claims" });
});
it("clears review when a selection changes", async () => {
  backend(); render(<SalesLibrary onChanged={vi.fn()} />); await select();
  fireEvent.change(screen.getByLabelText("Active claim library"), { target: { value: "" } });
  expect(screen.queryByRole("button", { name: "Use reviewed library" })).not.toBeInTheDocument();
});
it("requires reload after a conflict and does not retry selection", async () => {
  const fetcher = backend(true); render(<SalesLibrary onChanged={vi.fn()} />); await select();
  fireEvent.click(screen.getByRole("button", { name: "Use reviewed library" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/reload/i);
  expect(screen.getByRole("button", { name: "Review library selection" })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
});
it("pages published policies so later versions can be inspected and selected", async () => {
  vi.spyOn(global, "fetch").mockImplementation(async (url) => {
    if (String(url).includes("kind=qualification")) return Response.json(String(url).includes("after=")
      ? { items: [{ ...policy, version: "v2" }], next_cursor: null } : { items: [policy], next_cursor: "v1" });
    if (String(url).includes("kind=knowledge")) return Response.json({ items: [], next_cursor: null });
    return Response.json(empty);
  });
  render(<SalesLibrary onChanged={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Load more qualification versions" }));
  expect(await screen.findByRole("option", { name: "v2" })).toBeInTheDocument();
});
