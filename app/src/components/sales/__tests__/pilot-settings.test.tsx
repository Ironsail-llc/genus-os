import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PilotSettings } from "../pilot-settings";

const initial = { revision: 4, config: { monthly_limit_units: 100_000_000, daily_limit_units: 20_000_000,
  verification_allowance_units: 0, discovery_daily_limit: 20, review_backlog_limit: 100,
  mailbox_daily_limit: 5, discovery_start_hour: 2, discovery_end_hour: 7, timezone: "America/New_York",
  sending_enabled: false, agents: { scout: "sample-scout" } } };
afterEach(() => vi.restoreAllMocks());

it("allows reviewed request-only discovery without changing integration switches", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async () => Response.json(initial));
  render(<PilotSettings onChanged={vi.fn()} />);
  const mode = await screen.findByLabelText("Discovery scheduling");
  fireEvent.change(mode, { target: { value: "requests" } });
  fireEvent.change(screen.getByLabelText("Reason for these limits"), { target: { value: "Only discover against bounded research requests" } });
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  fireEvent.click(screen.getByRole("button", { name: "Save reviewed limits" }));
  await waitFor(() => expect(fetcher.mock.calls.some(([, options]) => options?.method === "POST")).toBe(true));
  const body = fetcher.mock.calls.find(([, options]) => options?.method === "POST")?.[1]?.body;
  expect(JSON.parse(String(body)).changes).toEqual({ discovery_mode: "requests" });
});

it("reviews an optional bounded follow-up cadence without enabling sending", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async () => Response.json(initial));
  render(<PilotSettings onChanged={vi.fn()} />);
  const cadence = await screen.findByLabelText("Follow-up delays (business days; blank disables)");
  fireEvent.change(cadence, { target: { value: "3, 4, 5" } });
  fireEvent.change(screen.getByLabelText("Reason for these limits"), { target: { value: "Review two follow-up delays" } });
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  expect(screen.getByRole("alert")).toHaveTextContent(/at most two/i);
  fireEvent.change(cadence, { target: { value: "3, 4" } });
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  fireEvent.click(screen.getByRole("button", { name: "Save reviewed limits" }));
  await waitFor(() => expect(fetcher.mock.calls.some(([, o]) => o?.method === "POST")).toBe(true));
  const body = fetcher.mock.calls.find(([, o]) => o?.method === "POST")?.[1]?.body;
  expect(JSON.parse(String(body)).changes).toEqual({ followup_delays_business_days: [3, 4] });
});

async function reviewMonthly(value = "500") {
  await screen.findByLabelText("Monthly spending limit (USD)");
  fireEvent.change(screen.getByLabelText("Monthly spending limit (USD)"), { target: { value } });
  fireEvent.change(screen.getByLabelText("Reason for these limits"), { target: { value: "Reviewed pilot spending ceiling" } });
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
}

it("shows a review before saving only changed limits against the displayed revision", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (_url, options) => Response.json(options?.method === "POST"
    ? { revision: 5, config: { ...initial.config, monthly_limit_units: 500_000_000 } } : initial));
  const changed = vi.fn();
  render(<PilotSettings onChanged={changed} />);
  await reviewMonthly();
  expect(screen.getByRole("region", { name: "Review pilot changes" })).toHaveTextContent("100 → 500");
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Save reviewed limits" }));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
  const writes = fetcher.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(writes).toHaveLength(1);
  expect(writes[0][0]).toContain("/settings/review");
  expect(JSON.parse(String(writes[0][1]?.body))).toEqual({ changes: { monthly_limit_units: 500_000_000 }, expected_revision: 4, reason: "Reviewed pilot spending ceiling" });
});

it("requires a new review after an edit and preserves exact micro-dollar input", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async () => Response.json(initial));
  render(<PilotSettings onChanged={vi.fn()} />);
  await reviewMonthly();
  fireEvent.change(screen.getByLabelText("Monthly spending limit (USD)"), { target: { value: "0.100001" } });
  expect(screen.queryByRole("button", { name: "Save reviewed limits" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  fireEvent.click(screen.getByRole("button", { name: "Save reviewed limits" }));
  await waitFor(() => expect(fetcher.mock.calls.some(([, options]) => options?.method === "POST")).toBe(true));
  const body = fetcher.mock.calls.find(([, options]) => options?.method === "POST")?.[1]?.body;
  expect(JSON.parse(String(body)).changes.monthly_limit_units).toBe(100001);
});

it("holds a failed write until refresh, then reloads the newer operator values for a fresh review", async () => {
  let current = initial;
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async (_url, options) => {
    if (options?.method === "POST") {
      current = { revision: 5, config: { ...initial.config, monthly_limit_units: 250_000_000 } };
      return Response.json({ detail: "Settings changed" }, { status: 409 });
    }
    return Response.json(current);
  });
  render(<PilotSettings onChanged={vi.fn()} />);
  await reviewMonthly();
  fireEvent.click(screen.getByRole("button", { name: "Save reviewed limits" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/reload current limits/i);
  expect(screen.getByRole("button", { name: "Review changes" })).toBeDisabled();
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Reload current limits" }));
  await waitFor(() => expect(screen.getByLabelText("Monthly spending limit (USD)")).toHaveValue("250"));
  expect(screen.queryByRole("button", { name: "Save reviewed limits" })).not.toBeInTheDocument();
});

it("rejects invalid hours and excessive decimal precision before a write", async () => {
  const fetcher = vi.spyOn(global, "fetch").mockImplementation(async () => Response.json(initial));
  render(<PilotSettings onChanged={vi.fn()} />);
  await reviewMonthly("0.0000001");
  expect(await screen.findByRole("alert")).toHaveTextContent(/six decimal/i);
  fireEvent.change(screen.getByLabelText("Monthly spending limit (USD)"), { target: { value: "500" } });
  fireEvent.change(screen.getByLabelText("Discovery starts at hour"), { target: { value: "9" } });
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  expect(screen.getByRole("alert")).toHaveTextContent(/before/i);
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
});
