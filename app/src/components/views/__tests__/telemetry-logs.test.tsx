/**
 * Observe › Logs — the organization's own lines in the shared Datadog account.
 *
 * Everyone who may open the page (an owner, admin or auditor of the acting
 * tenant) lands on the Datadog source. The journald "Server" source is the
 * appliance's, so it is offered to the root operator at home and to nobody
 * else — not to a child's admin, and not to a root admin acting in a child.
 *
 * The states are the bridge's (`crm/bridge/routers/telemetry.py`), each in
 * plain words: not connected, unavailable, a refused query with the server's
 * own reason, and an empty window. Search runs on submit, never per keystroke.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LogsView } from "../logs-view";

const ROWS = {
  status: "ok",
  scope: "acme-child-a",
  rows: [
    {
      id: "log-1",
      timestamp: "2026-11-25T10:00:00Z",
      service: "shop-api",
      status: "error",
      host: "web-1",
      message: "checkout failed",
      tags: ["env:prod", "service:shop-api"],
      attributes: { http: { status_code: 500 } },
    },
    {
      id: "log-2",
      timestamp: "2026-11-25T09:59:00Z",
      service: "clinic-app",
      status: "info",
      host: "web-2",
      message: "appointment booked",
      tags: [],
      attributes: {},
    },
  ],
  next_cursor: "cursor-2",
};

const PAGE_TWO = {
  status: "ok",
  scope: "acme-child-a",
  rows: [
    {
      id: "log-3",
      timestamp: "2026-11-25T09:58:00Z",
      service: "shop-api",
      status: "warn",
      host: "web-1",
      message: "slow checkout",
      tags: [],
      attributes: {},
    },
  ],
  next_cursor: null,
};

const SUMMARY = {
  status: "ok",
  scope: "acme-child-a",
  group_by: "service",
  errors: true,
  buckets: [
    { by: "shop-api", count: 7 },
    { by: "clinic-app", count: 2 },
  ],
};

const UNITS = {
  units: [{ name: "robothor-engine", description: "Genus OS Agent Engine (Python)" }],
  available: true,
};

const JOURNAL = { unit: "robothor-engine", available: true, lines: [], truncated: false };

type Reply = { status?: number; body: unknown };

const requests: string[] = [];
const fetchMock = vi.fn();

function respond(routes: Array<[RegExp, (url: string) => Reply]>) {
  fetchMock.mockImplementation(async (url: string) => {
    requests.push(url);
    for (const [pattern, make] of routes) {
      if (pattern.test(url)) {
        const { status = 200, body } = make(url);
        return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
      }
    }
    throw new Error(`no stub for ${url}`);
  });
}

const SUMMARY_URL = /\/api\/telemetry\/logs\/summary\?/;
const ROWS_URL = /\/api\/telemetry\/logs\?/;

function healthy(rows: (url: string) => unknown = (url) => (url.includes("cursor=") ? PAGE_TWO : ROWS)) {
  respond([
    [SUMMARY_URL, () => ({ body: SUMMARY })],
    [ROWS_URL, (url) => ({ body: rows(url) })],
    [/\/api\/logs\/units/, () => ({ body: UNITS })],
    [/\/api\/logs\?/, () => ({ body: JOURNAL })],
  ]);
}

function rowRequests(): string[] {
  return requests.filter((u) => ROWS_URL.test(u));
}

function summaryRequests(): string[] {
  return requests.filter((u) => SUMMARY_URL.test(u));
}

beforeEach(() => {
  requests.length = 0;
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Observe › Logs — Datadog", () => {
  it("reads the organization's last hour on arrival: rows and errors by service", async () => {
    healthy();
    render(<LogsView role="admin" />);

    const table = await screen.findByTestId("telemetry-table");
    expect(table.textContent).toContain("checkout failed");
    expect(table.textContent).toContain("shop-api");
    expect(table.textContent).toContain("error");

    const summary = await screen.findByTestId("telemetry-summary");
    expect(summary.textContent).toContain("Errors by service");
    expect(within(summary).getByTestId("telemetry-bucket-shop-api").textContent).toContain("7");
    expect(within(summary).getByTestId("telemetry-bucket-clinic-app").textContent).toContain("2");

    expect(rowRequests()[0]).toContain("since=1h");
    expect(summaryRequests()[0]).toContain("since=1h");
    // The bridge answers for the token's tenant; the page never names one.
    for (const url of requests) expect(url).not.toMatch(/tenant/);
  });

  it("offers a generic example in the search box", async () => {
    healthy();
    render(<LogsView role="admin" />);

    const box = await screen.findByTestId("telemetry-query");
    expect(box.getAttribute("placeholder")).toContain("status:error service:shop-api");
  });

  it("searches on submit, not on every keystroke", async () => {
    healthy();
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    const before = rowRequests().length;

    fireEvent.change(screen.getByTestId("telemetry-query"), { target: { value: "status:error" } });
    fireEvent.change(screen.getByTestId("telemetry-query"), {
      target: { value: "status:error env:prod" },
    });
    expect(rowRequests()).toHaveLength(before);

    fireEvent.submit(screen.getByTestId("telemetry-search"));
    await waitFor(() => expect(rowRequests().length).toBe(before + 1));
    const sent = new URL(rowRequests()[rowRequests().length - 1], "http://x");
    expect(sent.searchParams.get("q")).toBe("status:error env:prod");
    await waitFor(() =>
      expect(new URL(summaryRequests()[summaryRequests().length - 1], "http://x").searchParams.get("q")).toBe(
        "status:error env:prod",
      ),
    );
  });

  it("reads another window when the time range changes", async () => {
    healthy();
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");

    fireEvent.change(screen.getByTestId("telemetry-since"), { target: { value: "24h" } });

    await waitFor(() => expect(rowRequests()[rowRequests().length - 1]).toContain("since=24h"));
    await waitFor(() => expect(summaryRequests()[summaryRequests().length - 1]).toContain("since=24h"));
  });

  it("loads more with the cursor the bridge gave, and stops when there is none", async () => {
    healthy();
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");

    fireEvent.click(screen.getByTestId("telemetry-more"));

    await waitFor(() => expect(screen.getByTestId("telemetry-table").textContent).toContain("slow checkout"));
    expect(rowRequests()[rowRequests().length - 1]).toContain("cursor=cursor-2");
    // The first page is still there: more is appended, not swapped in.
    expect(screen.getByTestId("telemetry-table").textContent).toContain("checkout failed");
    expect(screen.queryByTestId("telemetry-more")).toBeNull();
  });

  it("opens a row's tags and attributes in the details sheet", async () => {
    healthy();
    render(<LogsView role="admin" />);
    fireEvent.click(await screen.findByTestId("telemetry-row-0"));

    const detail = await screen.findByTestId("telemetry-detail");
    expect(detail.textContent).toContain("checkout failed");
    expect(detail.textContent).toContain("env:prod");
    expect(detail.textContent).toContain("status_code");
    expect(detail.textContent).toContain("web-1");
  });

  it("says plainly when Datadog is not connected for the organization", async () => {
    respond([
      [
        /\/api\/telemetry\/logs/,
        () => ({
          body: {
            status: "not_configured",
            message: "Datadog is not connected for this organization yet.",
            scope: "acme-child-a",
          },
        }),
      ],
    ]);
    render(<LogsView role="admin" />);

    const state = await screen.findByTestId("telemetry-not-configured");
    expect(state.textContent).toContain("Datadog is not connected for this organization yet.");
    expect(screen.queryByTestId("telemetry-table")).toBeNull();
    expect(screen.queryByTestId("telemetry-empty")).toBeNull();
  });

  it("says when Datadog cannot be read just now, and why in plain words", async () => {
    respond([
      [
        /\/api\/telemetry\/logs/,
        () => ({ body: { status: "unavailable", kind: "rate_limit", scope: "acme-child-a" } }),
      ],
    ]);
    render(<LogsView role="admin" />);

    const state = await screen.findByTestId("telemetry-unavailable");
    expect(state.textContent).toMatch(/Datadog/);
    expect(state.textContent).toMatch(/too many requests|rate/i);
    expect(screen.queryByTestId("telemetry-empty")).toBeNull();
  });

  it("shows the server's reason for a refused query", async () => {
    healthy();
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");
    respond([
      [
        /\/api\/telemetry\/logs/,
        () => ({ status: 400, body: { detail: "the brackets or parentheses close out of order" } }),
      ],
    ]);

    fireEvent.change(screen.getByTestId("telemetry-query"), { target: { value: "a) OR (b" } });
    fireEvent.submit(screen.getByTestId("telemetry-search"));

    const refused = await screen.findByTestId("telemetry-refused");
    expect(refused.textContent).toContain("the brackets or parentheses close out of order");
    // A refusal says nothing about what is in the window.
    expect(screen.queryByTestId("telemetry-empty")).toBeNull();
    expect(screen.queryByTestId("telemetry-table")).toBeNull();
  });

  it("says when nothing matched in the window", async () => {
    respond([
      [SUMMARY_URL, () => ({ body: { ...SUMMARY, buckets: [] } })],
      [ROWS_URL, () => ({ body: { status: "ok", scope: "acme-child-a", rows: [], next_cursor: null } })],
    ]);
    render(<LogsView role="admin" />);

    expect(await screen.findByTestId("telemetry-empty")).toBeTruthy();
    expect(screen.queryByTestId("telemetry-more")).toBeNull();
    expect((await screen.findByTestId("telemetry-summary")).textContent).toMatch(/no errors/i);
  });

  it("renders a bridge refusal of the role as not this caller's, not as a failure", async () => {
    respond([[/\/api\/telemetry\/logs/, () => ({ status: 403, body: { detail: "role not authorized" } })]]);
    render(<LogsView role="admin" />);

    expect(await screen.findByTestId("telemetry-forbidden")).toBeTruthy();
    expect(screen.queryByTestId("telemetry-unavailable")).toBeNull();
  });
});

describe("Observe › Logs — who sees which source", () => {
  it("shows a tenant admin only the Datadog source, and never reads the journal", async () => {
    healthy();
    render(<LogsView role="admin" />);
    await screen.findByTestId("telemetry-table");

    expect(screen.queryByTestId("logs-source-server")).toBeNull();
    expect(requests.some((u) => /\/api\/logs(\/units|\?)/.test(u))).toBe(false);
  });

  it("shows an auditor the Datadog source too", async () => {
    healthy();
    render(<LogsView role="auditor" />);

    expect(await screen.findByTestId("telemetry-table")).toBeTruthy();
    expect(screen.queryByTestId("logs-source-server")).toBeNull();
  });

  it("does not offer the journal to an owner acting away from the root", async () => {
    healthy();
    render(<LogsView role="owner" rootAtHome={false} />);
    await screen.findByTestId("telemetry-table");

    expect(screen.queryByTestId("logs-source-server")).toBeNull();
  });

  it("offers the root operator at home the Server source, Datadog first", async () => {
    healthy();
    render(<LogsView role="owner" rootAtHome />);

    expect(await screen.findByTestId("telemetry-table")).toBeTruthy();
    const server = screen.getByTestId("logs-source-server");
    expect(screen.getByTestId("logs-source-datadog").getAttribute("aria-selected")).toBe("true");
    expect(requests.some((u) => /\/api\/logs\/units/.test(u))).toBe(false);

    fireEvent.click(server);

    expect(await screen.findByTestId("logs-unit")).toBeTruthy();
    expect(server.getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByTestId("telemetry-table")).toBeNull();
  });

  it("does not offer the journal to an auditor at the root either", async () => {
    healthy();
    render(<LogsView role="auditor" rootAtHome />);
    await screen.findByTestId("telemetry-table");

    expect(screen.queryByTestId("logs-source-server")).toBeNull();
  });

  it("tells a member whose screen this is, and asks the bridge nothing", async () => {
    healthy();
    render(<LogsView role="member" />);

    expect(await screen.findByTestId("telemetry-not-yours")).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not decide a role it has not been told yet", () => {
    healthy();
    render(<LogsView role={undefined} roleLoading />);
    expect(screen.queryByTestId("telemetry-not-yours")).toBeNull();
  });

  it("touches nothing while it is not the view on screen", () => {
    healthy();
    render(<LogsView role="admin" visible={false} />);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
