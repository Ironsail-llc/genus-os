/**
 * Observe › Audit — what was done, and who widened which guardrail.
 *
 * Two tabs over two routes that look alike and are not:
 *
 * * **Events** (`GET /api/audit/events`) has no cursor. It has a `limit`, and
 *   a failure that is a **200 with an `error` key** rather than a 500 — pinned
 *   deliberately on the bridge, so this page has to read the body before it
 *   believes an empty list. "Load more" is therefore limit growth, not a
 *   keyset, and it stops at the route's ceiling of 500.
 * * **Flag changes** (`GET /api/controls/audit`) is a keyset on `id DESC` and
 *   does have a `next_cursor`.
 *
 * The export is a plain `<a href download>` at `/api/audit/events.csv`, not a
 * fetch-and-blob: 5,000 rows through a JS string is a copy in memory for no
 * gain, and the browser already knows what to do with an attachment. It has to
 * carry the same filters the table is showing — an export that quietly ignored
 * the filters is worse than no export.
 *
 * And it says **appliance-wide**: `audit_log` has no tenant column, so the
 * rows are every tenant's, exactly as `GET /api/audit/events` has always been.
 * The tenant in the filename names who exported it, not what is inside.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuditView } from "../audit-view";

const EVENTS = {
  events: [
    {
      id: 91,
      timestamp: "2026-09-15T12:00:00+00:00",
      event_type: "memory.forget",
      category: "memory",
      actor: "operator:alice",
      action: "4711",
      target: "memory_facts",
      status: "success",
      source_channel: "helm",
      session_key: "sess-1",
      user_id: "u-1",
      details: '{"fact_id": 4711}',
    },
    {
      id: 90,
      timestamp: "2026-09-15T11:00:00+00:00",
      event_type: "tool.blocked",
      category: "guardrail",
      actor: "agent:bob",
      action: "gws_gmail_send",
      target: "gmail",
      status: "blocked",
      source_channel: "engine",
      session_key: null,
      user_id: null,
      details: "{}",
    },
  ],
  count: 2,
};

const CHANGES = {
  changes: [
    {
      id: 7,
      flag: "ROBOTHOR_RBAC_MODE",
      old_value: "observe",
      new_value: "enforce",
      changed_by: "operator:alice",
      reason: "soak is clean",
      changed_at: "2026-09-15T12:00:00+00:00",
    },
    {
      id: 6,
      flag: "ROBOTHOR_DNC_MODE",
      old_value: null,
      new_value: "observe",
      changed_by: "operator:alice",
      reason: null,
      changed_at: "2026-09-14T12:00:00+00:00",
    },
  ],
  next_cursor: "6",
};

const CHANGES_PAGE_TWO = {
  changes: [
    {
      id: 5,
      flag: "ROBOTHOR_JUDGE_ENABLED",
      old_value: "false",
      new_value: "true",
      changed_by: "operator:bob",
      reason: null,
      changed_at: "2026-09-13T12:00:00+00:00",
    },
  ],
  next_cursor: null,
};

const requests: string[] = [];
const fetchMock = vi.fn();

function respond(routes: Array<[RegExp, () => { status?: number; body: unknown }]>) {
  fetchMock.mockImplementation(async (url: string) => {
    requests.push(url);
    for (const [pattern, make] of routes) {
      if (pattern.test(url)) {
        const { status = 200, body } = make();
        return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
      }
    }
    throw new Error(`no stub for ${url}`);
  });
}

function bothRoutes() {
  respond([
    [/\/api\/controls\/audit/, () => ({ body: CHANGES })],
    [/\/api\/audit\/events/, () => ({ body: EVENTS })],
  ]);
}

function lastRequest(pattern: RegExp): string {
  const matched = requests.filter((url) => pattern.test(url));
  return matched[matched.length - 1] ?? "";
}

beforeEach(() => {
  requests.length = 0;
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Observe › Audit — events", () => {
  it("renders an event with its actor, action, target and status", async () => {
    bothRoutes();
    render(<AuditView role="owner" />);

    const row = await screen.findByTestId("audit-event-91");
    expect(row.textContent).toContain("memory.forget");
    expect(row.textContent).toContain("operator:alice");
    expect(row.textContent).toContain("success");
    expect(screen.getByTestId("audit-event-90").textContent).toContain("blocked");
  });

  it("puts every filter in the request", async () => {
    bothRoutes();
    render(<AuditView role="owner" />);
    await screen.findByTestId("audit-event-91");

    fireEvent.change(screen.getByTestId("audit-filter-event-type"), {
      target: { value: "memory.forget" },
    });
    fireEvent.change(screen.getByTestId("audit-filter-actor"), {
      target: { value: "operator:alice" },
    });
    fireEvent.change(screen.getByTestId("audit-filter-user"), { target: { value: "u-1" } });
    fireEvent.change(screen.getByTestId("audit-filter-since"), {
      target: { value: "2026-09-01" },
    });
    fireEvent.change(screen.getByTestId("audit-filter-until"), {
      target: { value: "2026-09-16" },
    });
    fireEvent.click(screen.getByTestId("audit-apply"));

    await waitFor(() => expect(lastRequest(/audit\/events/)).toContain("event_type=memory.forget"));
    const url = lastRequest(/audit\/events/);
    expect(url).toContain("actor=operator%3Aalice");
    expect(url).toContain("user_id=u-1");
    expect(url).toContain("since=2026-09-01");
    expect(url).toContain("until=2026-09-16");
  });

  it("refuses a time bound it knows the bridge will refuse", async () => {
    bothRoutes();
    render(<AuditView role="owner" />);
    await screen.findByTestId("audit-event-91");
    const before = requests.length;

    fireEvent.change(screen.getByTestId("audit-filter-since"), {
      target: { value: "yesterday" },
    });
    fireEvent.click(screen.getByTestId("audit-apply"));

    expect(screen.getByTestId("audit-since-error").textContent).toMatch(/ISO/i);
    expect(requests.length).toBe(before);
  });

  it("grows the limit rather than pretending to have a cursor", async () => {
    bothRoutes();
    render(<AuditView role="owner" />);
    await screen.findByTestId("audit-event-91");
    expect(lastRequest(/audit\/events/)).toContain("limit=50");
    // Two rows out of a 50-row window: the page is not full, so there is
    // nothing more to ask for.
    expect(screen.queryByTestId("audit-events-more")).toBeNull();

    // A full page is the only signal this route gives that more may exist.
    respond([
      [/\/api\/controls\/audit/, () => ({ body: CHANGES })],
      [
        /\/api\/audit\/events/,
        () => ({
          body: {
            events: Array.from({ length: 50 }, (_, i) => ({ ...EVENTS.events[0], id: 500 - i })),
            count: 50,
          },
        }),
      ],
    ]);
    fireEvent.click(screen.getByTestId("audit-refresh"));

    const more = await screen.findByTestId("audit-events-more");
    fireEvent.click(more);
    await waitFor(() => expect(lastRequest(/audit\/events/)).toContain("limit=100"));
  });

  it("believes the error key in a 200, not the empty list beside it", async () => {
    respond([
      [/\/api\/controls\/audit/, () => ({ body: CHANGES })],
      [/\/api\/audit\/events/, () => ({ body: { events: [], count: 0, error: "internal error" } })],
    ]);
    render(<AuditView role="owner" />);

    expect((await screen.findByTestId("audit-events-error")).textContent).toContain(
      "internal error"
    );
    // Not "no events recorded" — that would be a claim the server did not make.
    expect(screen.queryByTestId("audit-events-empty")).toBeNull();
  });

  it("exports through a download link that carries the filters", async () => {
    bothRoutes();
    render(<AuditView role="owner" />);
    await screen.findByTestId("audit-event-91");

    fireEvent.change(screen.getByTestId("audit-filter-event-type"), {
      target: { value: "memory.forget" },
    });
    fireEvent.change(screen.getByTestId("audit-filter-since"), {
      target: { value: "2026-09-01" },
    });
    fireEvent.click(screen.getByTestId("audit-apply"));

    const link = await screen.findByTestId("audit-export");
    await waitFor(() =>
      expect(link.getAttribute("href")).toContain("event_type=memory.forget")
    );
    expect(link.tagName).toBe("A");
    expect(link.getAttribute("href")).toContain("/api/bridge/api/audit/events.csv?");
    expect(link.getAttribute("href")).toContain("since=2026-09-01");
    expect(link.hasAttribute("download")).toBe(true);
    // No fetch-and-blob: the browser follows the link itself.
    expect(requests.some((u) => u.includes("events.csv"))).toBe(false);
  });

  it("exports an unfiltered log without a dangling question mark", async () => {
    bothRoutes();
    render(<AuditView role="owner" />);

    const link = await screen.findByTestId("audit-export");
    expect(link.getAttribute("href")).toBe("/api/bridge/api/audit/events.csv");
  });

  /**
   * The house rule, from `use-bridge-poll`'s own header: a 403 is the bridge
   * saying the listing belongs to somebody else, and a member must not be
   * shown red for it. The two views that hand-roll their fetch have to obey
   * the rule the hook states, and this one is reachable without a bridge
   * change — the Helm reads "operator" off the session role while the bridge
   * also requires the platform tenant and a human session.
   */
  it("renders a 403 as a refusal in the house style, not as a failure", async () => {
    respond([
      [/\/api\/controls\/audit/, () => ({ body: CHANGES })],
      [/\/api\/audit\/events/, () => ({ status: 403, body: { detail: "operator or auditor role required" } })],
    ]);
    render(<AuditView role="owner" />);

    expect(await screen.findByTestId("audit-forbidden")).toBeTruthy();
    expect(screen.queryByTestId("audit-events-error")).toBeNull();
    expect(screen.queryByTestId("audit-events-empty")).toBeNull();
  });

  it("says the export is appliance-wide, because audit_log has no tenant column", async () => {
    bothRoutes();
    render(<AuditView role="owner" />);
    expect((await screen.findByTestId("audit-export-note")).textContent).toMatch(
      /every tenant|appliance-wide/i
    );
  });
});

describe("Observe › Audit — flag changes", () => {
  it("does not read the other tab's route until that tab is open", async () => {
    bothRoutes();
    render(<AuditView role="owner" />);
    await screen.findByTestId("audit-event-91");

    expect(requests.some((u) => u.includes("/api/controls/audit"))).toBe(false);

    fireEvent.click(screen.getByTestId("audit-tab-flags"));
    await waitFor(() => expect(requests.some((u) => u.includes("/api/controls/audit"))).toBe(true));
  });

  it("renders old and new as two pills, with the reason and who typed it", async () => {
    bothRoutes();
    render(<AuditView role="owner" />);
    fireEvent.click(screen.getByTestId("audit-tab-flags"));

    const row = await screen.findByTestId("audit-change-7");
    expect(within(row).getByTestId("audit-old-7").textContent).toBe("observe");
    expect(within(row).getByTestId("audit-new-7").textContent).toBe("enforce");
    expect(row.textContent).toContain("soak is clean");
    expect(row.textContent).toContain("operator:alice");
  });

  it("says unset rather than None for a flag's first ever write", async () => {
    bothRoutes();
    render(<AuditView role="owner" />);
    fireEvent.click(screen.getByTestId("audit-tab-flags"));

    const row = await screen.findByTestId("audit-change-6");
    expect(within(row).getByTestId("audit-old-6").textContent).toMatch(/unset/i);
    expect(row.textContent).not.toContain("None");
    expect(row.textContent).not.toContain("null");
  });

  it("renders a 403 on the change log as a refusal too", async () => {
    respond([
      [/\/api\/controls\/audit/, () => ({ status: 403, body: { detail: "auditor role required" } })],
      [/\/api\/audit\/events/, () => ({ body: EVENTS })],
    ]);
    render(<AuditView role="owner" />);
    fireEvent.click(screen.getByTestId("audit-tab-flags"));

    expect(await screen.findByTestId("audit-forbidden")).toBeTruthy();
    expect(screen.queryByTestId("audit-changes-error")).toBeNull();
    expect(screen.queryByTestId("audit-changes-empty")).toBeNull();
  });

  it("filters by flag and pages with the keyset cursor", async () => {
    let page = 0;
    respond([
      [/\/api\/controls\/audit/, () => ({ body: page++ === 0 ? CHANGES : CHANGES_PAGE_TWO })],
      [/\/api\/audit\/events/, () => ({ body: EVENTS })],
    ]);
    render(<AuditView role="owner" />);
    fireEvent.click(screen.getByTestId("audit-tab-flags"));
    await screen.findByTestId("audit-change-7");

    fireEvent.click(screen.getByTestId("audit-changes-more"));
    await screen.findByTestId("audit-change-5");
    expect(lastRequest(/controls\/audit/)).toContain("cursor=6");
    expect(screen.getByTestId("audit-change-7")).toBeTruthy();
    await waitFor(() => expect(screen.queryByTestId("audit-changes-more")).toBeNull());

    fireEvent.change(screen.getByTestId("audit-flag-filter"), {
      target: { value: "ROBOTHOR_RBAC_MODE" },
    });
    fireEvent.click(screen.getByTestId("audit-flag-apply"));
    await waitFor(() =>
      expect(lastRequest(/controls\/audit/)).toContain("flag=ROBOTHOR_RBAC_MODE")
    );
    // A new filter starts a new list — the old cursor is not carried over.
    expect(lastRequest(/controls\/audit/)).not.toContain("cursor=");
  });
});

describe("Observe › Audit — who may look", () => {
  it("lets an auditor in — this is the screen the role exists for", async () => {
    bothRoutes();
    render(<AuditView role="auditor" />);
    expect(await screen.findByTestId("audit-event-91")).toBeTruthy();
    expect(screen.queryByTestId("audit-not-yours")).toBeNull();
  });

  it("keeps a member out, and asks the bridge nothing", async () => {
    bothRoutes();
    render(<AuditView role="member" />);
    expect(await screen.findByTestId("audit-not-yours")).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not decide a role it has not been told yet", () => {
    bothRoutes();
    render(<AuditView role={undefined} roleLoading />);
    expect(screen.queryByTestId("audit-not-yours")).toBeNull();
  });

  it("touches nothing while it is not the view on screen", () => {
    bothRoutes();
    render(<AuditView role="owner" visible={false} />);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
