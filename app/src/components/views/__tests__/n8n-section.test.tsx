/**
 * External: n8n — the operator's own n8n, watched from the Automations view.
 *
 * The claims: it lists each workflow with its failure counts and a link out to
 * n8n; a monitor that is switched off is one muted line, not an error; a member
 * sees the operator-only sentence, not red; an unreachable bridge says so; and
 * it does not touch the bridge while the view is hidden.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { N8nSection } from "../automations/n8n-section";
import { BRIDGE_UNREACHABLE, POLL_MS } from "@/lib/bridge/use-bridge-poll";

afterEach(() => vi.restoreAllMocks());

const LISTING = {
  status: "warn",
  base_url: "https://n8n.example.com",
  last_poll_at: "2026-06-15T12:00:00Z",
  last_ok_at: "2026-06-15T12:00:00Z",
  last_error: null,
  workflows: [
    {
      workflow_id: "wf1",
      name: "Invoice sync",
      active: true,
      tags: ["billing"],
      failures_24h: 2,
      failures_7d: 5,
      last_failure_at: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
      last_error: "timeout",
      last_failure_url: "https://n8n.example.com/workflow/wf1/executions/ex7",
      url: "https://n8n.example.com/workflow/wf1",
    },
    {
      workflow_id: "wf2",
      name: "Lead intake",
      active: false,
      tags: [],
      failures_24h: 0,
      failures_7d: 0,
      last_failure_at: null,
      last_error: null,
      last_failure_url: "",
      url: "https://n8n.example.com/workflow/wf2",
    },
  ],
};

function reply(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ "content-type": "application/json" }),
  } as unknown as Response;
}

describe("N8nSection", () => {
  it("reads the n8n listing through the bridge proxy", async () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue(reply(LISTING));
    render(<N8nSection visible />);
    await screen.findAllByTestId("n8n-row");
    expect(spy).toHaveBeenCalledWith(
      "/api/bridge/api/integrations/n8n/workflows",
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );
  });

  it("renders a row per workflow with counts, active pill and a link out", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(LISTING));
    render(<N8nSection visible />);
    const rows = await screen.findAllByTestId("n8n-row");
    expect(rows).toHaveLength(2);

    const first = within(rows[0]);
    expect(first.getByText("Invoice sync")).toBeTruthy();
    expect(rows[0].textContent).toMatch(/active/i);
    expect(rows[0].textContent).toMatch(/2\s*\/\s*5/);
    expect(rows[0].textContent).toMatch(/3 hours ago/);
    const link = first.getByRole("link", { name: /open invoice sync in n8n/i });
    expect(link.getAttribute("href")).toBe("https://n8n.example.com/workflow/wf1");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");

    expect(rows[1].textContent).toMatch(/inactive/i);
  });

  it("headlines the section with its status", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(LISTING));
    render(<N8nSection visible />);
    await screen.findAllByTestId("n8n-row");
    const section = screen.getByTestId("n8n-section");
    expect(section.textContent).toContain("External: n8n");
    expect(within(section).getByTestId("n8n-status").textContent).toMatch(/warn/i);
  });

  it("a switched-off monitor is one muted line pointing at the docs", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      reply({ ...LISTING, status: "not_configured", workflows: [] })
    );
    render(<N8nSection visible />);
    const line = await screen.findByTestId("n8n-not-configured");
    expect(line.textContent).toContain("n8n monitoring is not configured");
    expect(line.textContent).toContain("docs/integrations/n8n.md");
    expect(screen.queryAllByTestId("n8n-row")).toHaveLength(0);
  });

  it("a database error the bridge names is shown, not an empty healthy list", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      reply({
        ...LISTING,
        status: "unknown",
        last_error: "relation n8n_workflows does not exist",
        workflows: [],
      })
    );
    render(<N8nSection visible />);
    const line = await screen.findByTestId("n8n-last-error");
    expect(line.textContent).toContain("relation n8n_workflows does not exist");
    expect(within(await screen.findByTestId("n8n-status")).getByText("Unknown")).toBeTruthy();
  });

  it("an operator-only refusal is the forbidden sentence, not an error", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply({ detail: "operator role required" }, 403));
    render(<N8nSection visible />);
    expect(await screen.findByTestId("n8n-forbidden")).toBeTruthy();
    expect(screen.queryByTestId("n8n-error")).toBeNull();
  });

  it("an unreachable bridge says so", async () => {
    vi.spyOn(global, "fetch").mockRejectedValue(new TypeError("fetch failed"));
    render(<N8nSection visible />);
    const err = await screen.findByTestId("n8n-error");
    expect(err.textContent).toContain(BRIDGE_UNREACHABLE);
  });

  it("links the last failure to the execution that failed", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(LISTING));
    render(<N8nSection visible />);
    const rows = await screen.findAllByTestId("n8n-row");
    const failure = within(rows[0]).getByTestId("n8n-last-failure");
    expect(failure.tagName).toBe("A");
    expect(failure.getAttribute("href")).toBe(
      "https://n8n.example.com/workflow/wf1/executions/ex7"
    );
    expect(failure.getAttribute("target")).toBe("_blank");
    expect(failure.getAttribute("rel")).toContain("noopener");
    expect(failure.textContent).toMatch(/3 hours ago/);
  });

  it("a last failure without an execution link is plain text", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      reply({
        ...LISTING,
        workflows: [{ ...LISTING.workflows[0], last_failure_url: "" }],
      })
    );
    render(<N8nSection visible />);
    const [row] = await screen.findAllByTestId("n8n-row");
    expect(within(row).getByTestId("n8n-last-failure").tagName).not.toBe("A");
  });

  it("does not fetch when hidden", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue(reply(LISTING));
    render(<N8nSection visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("N8nSection polling", () => {
  afterEach(() => vi.useRealTimers());

  it("stops polling once the monitor says it is not configured", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const spy = vi
      .spyOn(global, "fetch")
      .mockResolvedValue(reply({ ...LISTING, status: "not_configured", workflows: [] }));
    render(<N8nSection visible />);
    await screen.findByTestId("n8n-not-configured");
    await vi.advanceTimersByTimeAsync(0);
    const settled = spy.mock.calls.length;

    await vi.advanceTimersByTimeAsync(POLL_MS * 3 + 1_000);
    expect(spy.mock.calls.length).toBe(settled);
  });

  it("keeps polling while the monitor is configured", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const spy = vi.spyOn(global, "fetch").mockResolvedValue(reply(LISTING));
    render(<N8nSection visible />);
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(POLL_MS + 1_000);
    expect(spy.mock.calls.length).toBeGreaterThan(1);
  });
});
