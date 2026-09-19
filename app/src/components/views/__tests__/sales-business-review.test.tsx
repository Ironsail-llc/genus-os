import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BusinessReview } from "@/components/sales/business-review";

afterEach(() => vi.restoreAllMocks());
const practice = { id: "observed-1", source: "orders_app", account_id: "account-1", external_id: "practice-1",
  revision: "revision-shown", observed_at: "2026-09-18T12:00:00Z", data: { name: "East Clinic", state: "TX", active: true, business_unit_id: "group-1" },
  prospect_id: null, binding_status: null };
const props = { prospects: [{ id: "prospect-1", name: "Example Customer", domain: "example.com" }], sources: [], onChanged: vi.fn(async () => {}) };
const response = (value: unknown) => ({ ok: true, json: async () => value }) as Response;

describe("Business identity review", () => {
  it("ignores a previous source response after the source selection changes", async () => {
    let release!: (value: Response) => void;
    vi.spyOn(global, "fetch").mockImplementation(async (path) => String(path).includes("account_id=account-2")
      ? response({ items: [{ ...practice, id: "new-practice", data: { ...practice.data, name: "Current Clinic" } }], next_cursor: null })
      : new Promise<Response>((resolve) => { release = resolve; }));
    render(<BusinessReview {...props} sources={[{ source: "orders_app", account_id: "account-2" }]} />);
    fireEvent.change(screen.getByLabelText("Practice source"), { target: { value: "orders_app:account-2" } });
    expect(await screen.findByText("Current Clinic")).toBeInTheDocument();
    await act(async () => release(response({ items: [practice], next_cursor: null })));
    expect(screen.queryByText("East Clinic")).not.toBeInTheDocument();
  });

  it("preserves the cursor and existing records after a failed next-page read", async () => {
    let fail = true;
    vi.spyOn(global, "fetch").mockImplementation(async (path) => {
      if (!String(path).includes("after=")) return response({ items: [practice], next_cursor: "cursor-1" });
      if (fail) return { ok: false, status: 503, statusText: "Unavailable" } as Response;
      return response({ items: [{ ...practice, id: "observed-2", data: { ...practice.data, name: "West Clinic" } }], next_cursor: null });
    });
    render(<BusinessReview {...props} />);
    fireEvent.click(await screen.findByRole("button", { name: "Load more practices" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("503");
    expect(screen.getByText("East Clinic")).toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Load more practices" }));
    expect(await screen.findByText("West Clinic")).toBeInTheDocument();
    expect(screen.getByText("East Clinic")).toBeInTheDocument();
  });

  it("requires explicit identity review and submits the displayed revision and selected customer", async () => {
    const fetcher = vi.spyOn(global, "fetch").mockResolvedValue(response({ items: [practice], next_cursor: null }));
    render(<BusinessReview {...props} />);
    fireEvent.click(await screen.findByRole("button", { name: "Review match for East Clinic" }));
    const confirm = screen.getByRole("button", { name: "Confirm practice match" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Genus customer"), { target: { value: "prospect-1" } });
    fireEvent.change(screen.getByLabelText("Matching evidence and reason"), { target: { value: "Verified company ownership and practice location" } });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByLabelText("I verified that this practice belongs to this customer"));
    fireEvent.click(confirm);
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith(
      "/api/bridge/api/sales/prospects/prospect-1/business-customer", expect.objectContaining({ method: "POST",
        body: JSON.stringify({ observation_id: "observed-1", expected_revision: "revision-shown", reason: "Verified company ownership and practice location" }) })));
    expect(await screen.findByRole("status")).toHaveTextContent("Practice match saved");
  });

  it("invalidates a stale review and does not claim it was saved", async () => {
    vi.spyOn(global, "fetch").mockImplementation(async (_path, options) => options?.method === "POST"
      ? ({ ok: false, status: 409, statusText: "Conflict" }) as Response : response({ items: [practice], next_cursor: null }));
    render(<BusinessReview {...props} />);
    fireEvent.click(await screen.findByRole("button", { name: "Review match for East Clinic" }));
    fireEvent.change(screen.getByLabelText("Genus customer"), { target: { value: "prospect-1" } });
    fireEvent.change(screen.getByLabelText("Matching evidence and reason"), { target: { value: "Verified company and location" } });
    fireEvent.click(screen.getByLabelText("I verified that this practice belongs to this customer"));
    fireEvent.click(screen.getByRole("button", { name: "Confirm practice match" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/refresh.*review again/i);
    expect(screen.queryByRole("button", { name: "Confirm practice match" })).not.toBeInTheDocument();
    expect(screen.queryByText("Practice match saved.")).not.toBeInTheDocument();
  });

  it("paginates observations and prevents reassignment to a different customer", async () => {
    const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (path) => response(String(path).includes("after=")
      ? { items: [{ ...practice, id: "observed-2", data: { ...practice.data, name: "West Clinic" }, prospect_id: "different-customer", binding_status: "confirmed" }], next_cursor: null }
      : { items: [practice], next_cursor: "cursor-1" }));
    render(<BusinessReview {...props} />);
    fireEvent.click(await screen.findByRole("button", { name: "Load more practices" }));
    fireEvent.click(await screen.findByRole("button", { name: "Review match for West Clinic" }));
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("after=cursor-1"), expect.anything());
    fireEvent.change(screen.getByLabelText("Genus customer"), { target: { value: "prospect-1" } });
    fireEvent.change(screen.getByLabelText("Matching evidence and reason"), { target: { value: "Reviewed location evidence" } });
    fireEvent.click(screen.getByLabelText("I verified that this practice belongs to this customer"));
    expect(screen.getByRole("button", { name: "Confirm practice match" })).toBeDisabled();
    expect(screen.getByText(/already matched to another customer/i)).toBeInTheDocument();
  });
});
