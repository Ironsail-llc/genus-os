import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { onlineManager } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CommandCentreView } from "../command-centre-view";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  hide(false);
});

/** Stand in for the browser hiding or showing the tab. */
function hide(hidden: boolean) {
  Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
  Object.defineProperty(document, "visibilityState", {
    value: hidden ? "hidden" : "visible",
    configurable: true,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

const PARENT = {
  tenant_id: "acme-parent",
  display_name: "Acme Holdings",
  parent_id: null,
  runs_24h: 4,
  failures_24h: 0,
  success_rate_24h: 1,
  cost_usd_24h: 0.1,
  runs_7d: 20,
  failures_7d: 0,
  success_rate_7d: 1,
  cost_usd_7d: 0.9,
  top_failures: [],
  agents_with_consecutive_errors: [],
  overdue_schedules: [],
  tasks: { open: 2, overdue: 0, oldest_overdue: [] },
  last_activity_at: "2026-11-18T11:00:00+00:00",
  status: "ok",
};

const CHILD_A = {
  tenant_id: "acme-child-a",
  display_name: "Acme Child A",
  parent_id: "acme-parent",
  runs_24h: 10,
  failures_24h: 2,
  success_rate_24h: 0.8,
  cost_usd_24h: 0.5,
  runs_7d: 70,
  failures_7d: 10,
  success_rate_7d: 0.8571,
  cost_usd_7d: 3.25,
  top_failures: [{ agent_id: "triage", error_type: "timeout", count: 2 }],
  agents_with_consecutive_errors: [
    { agent_id: "triage", consecutive_errors: 3, last_status: "failed" },
  ],
  overdue_schedules: [
    { agent_id: "triage", next_run_at: "2026-11-18T09:00:00+00:00", overdue_minutes: 180 },
  ],
  tasks: {
    open: 7,
    overdue: 1,
    oldest_overdue: [
      {
        id: "11111111-1111-1111-1111-111111111111",
        title: "Renew the lease",
        status: "TODO",
        assignedToAgent: "main",
        dueAt: "2026-11-13T12:00:00+00:00",
        overdueDays: 5,
        ageDays: 12,
      },
    ],
  },
  last_activity_at: "2026-11-18T11:55:00+00:00",
  status: "attention",
};

const CHILD_B_ERROR = {
  tenant_id: "acme-child-b",
  display_name: "Acme Child B",
  parent_id: "acme-parent",
  status: "error",
  error: "could not read this tenant (OperationalError)",
};

function overview(tenants: unknown[], extra: Record<string, unknown> = {}) {
  return {
    as_of: "2026-11-18T12:00:00+00:00",
    home_tenant_id: "acme-parent",
    has_children: tenants.length > 1,
    switching_enabled: true,
    tenants,
    ...extra,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function routeFetch(routes: Record<string, () => Response>) {
  return vi.spyOn(global, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    for (const [path, reply] of Object.entries(routes)) {
      if (url.endsWith(path)) return reply();
    }
    return jsonResponse({ error: "unexpected" }, 500);
  });
}

const OVERVIEW_URL = "/api/bridge/api/org/overview";

describe("CommandCentreView", () => {
  it("shows one card per company, with the companies needing attention first", async () => {
    routeFetch({ [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A, CHILD_B_ERROR])) });

    render(<CommandCentreView visible />);

    const cards = await screen.findAllByTestId(/^company-card-/);
    expect(cards.map((c) => c.getAttribute("data-testid"))).toEqual([
      "company-card-acme-child-a",
      "company-card-acme-child-b",
      "company-card-acme-parent",
    ]);
  });

  it("names each company's status in words", async () => {
    routeFetch({ [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A, CHILD_B_ERROR])) });

    render(<CommandCentreView visible />);

    const a = await screen.findByTestId("company-card-acme-child-a");
    expect(within(a).getByText("Needs attention")).toBeTruthy();
    expect(within(screen.getByTestId("company-card-acme-parent")).getByText("OK")).toBeTruthy();
    expect(within(screen.getByTestId("company-card-acme-child-b")).getByText("Could not read")).toBeTruthy();
  });

  it("shows the figures a company needs attention for", async () => {
    routeFetch({ [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A])) });

    render(<CommandCentreView visible />);

    const card = await screen.findByTestId("company-card-acme-child-a");
    const text = card.textContent ?? "";
    expect(text).toContain("Acme Child A");
    expect(within(card).getByTestId("runs-24h").textContent).toContain("10");
    expect(within(card).getByTestId("failures-24h").textContent).toContain("2");
    expect(within(card).getByTestId("success-7d").textContent).toContain("86%");
    expect(within(card).getByTestId("cost").textContent).toContain("$3.25");
    // The failing agent with its classified reason.
    expect(within(card).getByTestId("top-failures").textContent).toMatch(/triage.*timeout.*2/);
    // Consecutive errors and the overdue schedule.
    expect(within(card).getByTestId("consecutive-errors").textContent).toMatch(/triage.*3/);
    expect(within(card).getByTestId("overdue-schedules").textContent).toMatch(/triage/);
    // Open and overdue tasks, and the oldest overdue one with its age in days.
    expect(within(card).getByTestId("tasks").textContent).toMatch(/7 open.*1 overdue/);
    const oldest = within(card).getByTestId("oldest-overdue").textContent ?? "";
    expect(oldest).toContain("Renew the lease");
    expect(oldest).toMatch(/5 days? overdue/);
    expect(oldest).toMatch(/12 days? old/);
    expect(within(card).getByTestId("last-activity")).toBeTruthy();
  });

  it("shows the time the whole page is as of", async () => {
    routeFetch({ [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A])) });

    render(<CommandCentreView visible />);

    const asOf = await screen.findByTestId("org-as-of");
    expect(asOf.getAttribute("dateTime")).toBe("2026-11-18T12:00:00+00:00");
  });

  it("says plainly that a company could not be read, instead of showing zeros", async () => {
    routeFetch({ [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_B_ERROR])) });

    render(<CommandCentreView visible />);

    const card = await screen.findByTestId("company-card-acme-child-b");
    expect(card.textContent).toContain("could not read this tenant");
    expect(within(card).queryByTestId("runs-24h")).toBeNull();
    expect(within(card).queryByTestId("tasks")).toBeNull();
  });

  it("shows an empty state, not a page of one card, when the account has no child companies", async () => {
    routeFetch({
      [OVERVIEW_URL]: () => jsonResponse(overview([PARENT], { has_children: false })),
    });

    render(<CommandCentreView visible />);

    expect(await screen.findByTestId("org-empty")).toBeTruthy();
    expect(screen.queryAllByTestId(/^company-card-/)).toEqual([]);
  });

  it("shows a loading skeleton while it fetches", () => {
    vi.spyOn(global, "fetch").mockReturnValue(new Promise(() => {}) as never);
    render(<CommandCentreView visible />);
    expect(screen.getByTestId("org-loading")).toBeTruthy();
  });

  it("explains a refusal", async () => {
    routeFetch({ [OVERVIEW_URL]: () => jsonResponse({ detail: "no" }, 403) });

    render(<CommandCentreView visible />);

    expect(await screen.findByTestId("org-error")).toBeTruthy();
  });

  it("says the bridge could not be reached when the request fails", async () => {
    vi.spyOn(global, "fetch").mockRejectedValue(new TypeError("network"));

    render(<CommandCentreView visible />);

    expect((await screen.findByTestId("org-error")).textContent).toContain("Could not reach");
  });

  it("does not fetch while hidden", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue(jsonResponse(overview([PARENT])));
    render(<CommandCentreView visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });

  it("switches into a company when its card is clicked and switching is on", async () => {
    const fetchMock = routeFetch({
      [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A])),
      "/api/session/switch-tenant": () =>
        jsonResponse({ tenant: "acme-child-a", homeTenant: "acme-parent" }),
    });
    const onSwitched = vi.fn();

    render(<CommandCentreView visible onSwitched={onSwitched} />);

    const open = await screen.findByRole("button", { name: /Open Acme Child A/ });
    fireEvent.click(open);

    await waitFor(() => expect(onSwitched).toHaveBeenCalled());
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/api/session/switch-tenant"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ tenant: "acme-child-a" });
  });

  it("does not offer a switch into the home company itself", async () => {
    routeFetch({ [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A])) });

    render(<CommandCentreView visible />);

    await screen.findByTestId("company-card-acme-parent");
    expect(screen.queryByRole("button", { name: /Open Acme Holdings/ })).toBeNull();
  });

  it("is not a link when switching is off", async () => {
    routeFetch({
      [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A], { switching_enabled: false })),
    });

    render(<CommandCentreView visible />);

    await screen.findByTestId("company-card-acme-child-a");
    expect(screen.queryByRole("button", { name: /Open Acme Child A/ })).toBeNull();
  });

  it("shows a refused switch on the card", async () => {
    routeFetch({
      [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A])),
      "/api/session/switch-tenant": () => jsonResponse({ error: "tenant not accessible" }, 403),
    });
    const onSwitched = vi.fn();

    render(<CommandCentreView visible onSwitched={onSwitched} />);

    fireEvent.click(await screen.findByRole("button", { name: /Open Acme Child A/ }));

    expect((await screen.findByRole("alert")).textContent).toContain("tenant not accessible");
    expect(onSwitched).not.toHaveBeenCalled();
  });

  describe("loads once when opened, and again only on Refresh", () => {
    // Every read of the overview writes an audit row in each child company,
    // so returning to the tab or the network coming back must not re-read it.
    function overviewCalls(spy: ReturnType<typeof routeFetch>) {
      return spy.mock.calls.filter(([url]) => String(url).endsWith(OVERVIEW_URL)).length;
    }

    it("does not read the overview again when the tab regains focus, however old it is", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const spy = routeFetch({ [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A])) });

      render(<CommandCentreView visible />);
      await screen.findByTestId("company-card-acme-child-a");
      expect(overviewCalls(spy)).toBe(1);

      // Well past the minute the answer stays fresh for.
      await act(async () => {
        vi.advanceTimersByTime(10 * 60_000);
      });
      await act(async () => {
        hide(true);
        hide(false);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });

      expect(overviewCalls(spy)).toBe(1);
    });

    it("does not read the overview again when the network comes back", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const spy = routeFetch({ [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A])) });

      render(<CommandCentreView visible />);
      await screen.findByTestId("company-card-acme-child-a");

      await act(async () => {
        vi.advanceTimersByTime(10 * 60_000);
      });
      await act(async () => {
        onlineManager.setOnline(false);
        onlineManager.setOnline(true);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });

      expect(overviewCalls(spy)).toBe(1);
    });

    it("reads it exactly once more when Refresh is clicked", async () => {
      const spy = routeFetch({ [OVERVIEW_URL]: () => jsonResponse(overview([PARENT, CHILD_A])) });

      render(<CommandCentreView visible />);
      await screen.findByTestId("company-card-acme-child-a");
      expect(overviewCalls(spy)).toBe(1);

      const refresh = screen.getByRole("button", { name: "Refresh" });
      fireEvent.click(refresh);

      await waitFor(() => expect(overviewCalls(spy)).toBe(2));
      await waitFor(() => expect(refresh.hasAttribute("disabled")).toBe(false));
      expect(overviewCalls(spy)).toBe(2);
      // The figures stay on screen while it reads.
      expect(screen.getByTestId("company-card-acme-child-a")).toBeTruthy();
    });

    it("disables Refresh and marks it busy while the overview is being read", async () => {
      let release: (r: Response) => void = () => {};
      let calls = 0;
      vi.spyOn(global, "fetch").mockImplementation(async (input) => {
        if (!String(input).endsWith(OVERVIEW_URL)) return jsonResponse({ error: "unexpected" }, 500);
        calls += 1;
        if (calls === 1) return jsonResponse(overview([PARENT, CHILD_A]));
        return new Promise<Response>((resolve) => {
          release = resolve;
        });
      });

      render(<CommandCentreView visible />);
      await screen.findByTestId("company-card-acme-child-a");

      const refresh = screen.getByRole("button", { name: "Refresh" });
      expect(refresh.hasAttribute("disabled")).toBe(false);

      fireEvent.click(refresh);

      await waitFor(() => expect(refresh.hasAttribute("disabled")).toBe(true));
      expect(refresh.getAttribute("aria-busy")).toBe("true");
      // A second click while it reads does not start another read.
      fireEvent.click(refresh);
      expect(calls).toBe(2);

      await act(async () => {
        release(jsonResponse(overview([PARENT, CHILD_A])));
      });
      await waitFor(() => expect(refresh.hasAttribute("disabled")).toBe(false));
      expect(refresh.getAttribute("aria-busy")).toBe("false");
    });
  });
});
