/**
 * Observe › Logs — journald, for an operator who is not on the box.
 *
 * The claim this file exists for is the LAST one: **`available: false` is a
 * 200**. A container has no journald and there is nothing broken about that,
 * so both routes answer with a sentence saying why, and a page that rendered
 * it in red would be reporting an appliance fault where there is none. Every
 * deployment of this app that runs in a container sees that state and only
 * that state, which makes it the most-seen screen here and the easiest one to
 * get wrong.
 *
 * The rest:
 *
 * * the unit picker is the route's own allowlist, never a list typed here;
 * * `truncated` is measured against what journald RETURNED, not what survived
 *   `grep`, so the notice says "window full", not "no more matches";
 * * priority is syslog's: ≤3 is an error, 4 is a warning, and anything else is
 *   neither — a page that coloured 6 would be crying wolf on every line;
 * * a `since` the bridge would refuse never leaves the page, because the same
 *   pattern is checked here first — and a refusal that does come back lands
 *   under the field that caused it;
 * * auto-refresh is OFF by default and pauses when the view is hidden. Ten
 *   seconds of journald is a real read on the box, and the shell keeps every
 *   view mounted.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LogsView } from "../logs-view";

const UNITS = {
  units: [
    { name: "robothor-engine", description: "Genus OS Agent Engine (Python)" },
    { name: "robothor-bridge", description: "Genus OS CRM bridge" },
  ],
  available: true,
};

const NO_JOURNALD = {
  units: [],
  available: false,
  reason: "journald is not available on this deployment (journalctl is not installed)",
};

const LINES = {
  unit: "robothor-engine",
  available: true,
  lines: [
    { ts: "2026-09-15T12:00:00+00:00", priority: 6, message: "engine started" },
    { ts: "2026-09-15T12:00:01+00:00", priority: 4, message: "provider slow, retrying" },
    { ts: "2026-09-15T12:00:02+00:00", priority: 3, message: "run failed: timeout" },
    { ts: null, priority: null, message: "a line journald gave no metadata for" },
  ],
  truncated: true,
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

/** `/api/logs/units` first — `/api/logs?` must not match the units route. */
function healthy(lines: unknown = LINES) {
  respond([
    [/\/api\/logs\/units/, () => ({ body: UNITS })],
    [/\/api\/logs\?/, () => ({ body: lines })],
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
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Observe › Logs", () => {
  it("offers the units the route allows, and reads the first one", async () => {
    healthy();
    render(<LogsView role="owner" />);

    const picker = await screen.findByTestId("logs-unit");
    expect(optionsOf(picker).length).toBe(2);
    await waitFor(() => expect(lastRequest(/\/api\/logs\?/)).toContain("unit=robothor-engine"));

    fireEvent.change(picker, { target: { value: "robothor-bridge" } });
    await waitFor(() => expect(lastRequest(/\/api\/logs\?/)).toContain("unit=robothor-bridge"));
  });

  it("renders a line with its time, priority and message", async () => {
    healthy();
    render(<LogsView role="owner" />);

    const pane = await screen.findByTestId("logs-pane");
    expect(pane.textContent).toContain("engine started");
    expect(pane.textContent).toContain("run failed: timeout");
  });

  it("colours by syslog priority — error at 3 and below, warning at 4, nothing else", async () => {
    healthy();
    render(<LogsView role="owner" />);
    await screen.findByTestId("logs-pane");

    expect(screen.getByTestId("logs-priority-2")).toHaveAttribute("data-level", "error");
    expect(screen.getByTestId("logs-priority-1")).toHaveAttribute("data-level", "warning");
    expect(screen.getByTestId("logs-priority-0")).toHaveAttribute("data-level", "info");
    // journald gave no PRIORITY for this one; inventing one would be a claim.
    expect(screen.getByTestId("logs-priority-3")).toHaveAttribute("data-level", "unknown");
  });

  it("says the window was full rather than that there is nothing more", async () => {
    healthy();
    render(<LogsView role="owner" />);

    const notice = await screen.findByTestId("logs-truncated");
    expect(notice.textContent?.toLowerCase()).toMatch(/window|raise|narrow/);
  });

  it("carries lines, since and grep in the request", async () => {
    healthy();
    render(<LogsView role="owner" />);
    await screen.findByTestId("logs-pane");

    fireEvent.change(screen.getByTestId("logs-lines"), { target: { value: "1000" } });
    await waitFor(() => expect(lastRequest(/\/api\/logs\?/)).toContain("lines=1000"));

    fireEvent.click(screen.getByTestId("logs-since-1h"));
    await waitFor(() => expect(lastRequest(/\/api\/logs\?/)).toContain("since=1h"));

    fireEvent.change(screen.getByTestId("logs-grep"), { target: { value: "timeout" } });
    fireEvent.click(screen.getByTestId("logs-refresh"));
    await waitFor(() => expect(lastRequest(/\/api\/logs\?/)).toContain("grep=timeout"));
  });

  it("refuses a since the bridge would refuse, without asking it", async () => {
    healthy();
    render(<LogsView role="owner" />);
    await screen.findByTestId("logs-pane");
    const before = requests.length;

    fireEvent.change(screen.getByTestId("logs-since-custom"), {
      target: { value: "2 hours ago" },
    });
    fireEvent.click(screen.getByTestId("logs-refresh"));

    expect(screen.getByTestId("logs-since-error")).toBeTruthy();
    expect(requests.length).toBe(before);

    // A relative age in journald's own units is fine, and goes.
    fireEvent.change(screen.getByTestId("logs-since-custom"), { target: { value: "45m" } });
    fireEvent.click(screen.getByTestId("logs-refresh"));
    await waitFor(() => expect(lastRequest(/\/api\/logs\?/)).toContain("since=45m"));
    expect(screen.queryByTestId("logs-since-error")).toBeNull();
  });

  it("lands a 422 from the bridge under the field that caused it", async () => {
    respond([
      [/\/api\/logs\/units/, () => ({ body: UNITS })],
      [
        /\/api\/logs\?/,
        () => ({ status: 422, body: { detail: "since must be a relative age or an ISO-8601 date" } }),
      ],
    ]);
    render(<LogsView role="owner" />);

    await waitFor(() =>
      expect(screen.getByTestId("logs-since-error").textContent).toContain("since must be")
    );
    // Not a red banner over an empty pane: the operator can fix this.
    expect(screen.queryByTestId("logs-error")).toBeNull();
  });

  /**
   * There is exactly one field slot on this page, under the custom `since`
   * box. A refusal routed at a field that has no slot used to be recognised,
   * placed nowhere, and silently suppress the empty state as well — so the
   * page answered a 422 with a blank screen. Every server answer has to land
   * SOMEWHERE, and the banner is where the ones with no field go.
   */
  it("puts a 422 with no field slot in the banner rather than nowhere", async () => {
    respond([
      [/\/api\/logs\/units/, () => ({ body: UNITS })],
      [
        /\/api\/logs\?/,
        () => ({ status: 422, body: { detail: "lines must be between 1 and 1000" } }),
      ],
    ]);
    render(<LogsView role="owner" />);

    expect((await screen.findByTestId("logs-error")).textContent).toContain("lines must be");
    expect(screen.queryByTestId("logs-empty")).toBeNull();
  });

  it("puts a refusal naming the unit in the banner too", async () => {
    respond([
      [/\/api\/logs\/units/, () => ({ body: UNITS })],
      [
        /\/api\/logs\?/,
        () => ({ status: 422, body: { detail: "unit must be one of: robothor-engine" } }),
      ],
    ]);
    render(<LogsView role="owner" />);

    expect((await screen.findByTestId("logs-error")).textContent).toContain("unit must be one of");
  });

  /**
   * The Helm decides "operator" from the session role; the bridge ALSO requires
   * the platform tenant and a human session, so an owner outside that tenant is
   * an operator here and a 403 there. The page must not answer that with a
   * claim about the journal.
   */
  it("says a 403 on the read is a refusal, not an empty journal", async () => {
    respond([
      [/\/api\/logs\/units/, () => ({ body: UNITS })],
      [/\/api\/logs\?/, () => ({ status: 403, body: { detail: "operator role required" } })],
    ]);
    render(<LogsView role="owner" />);

    expect(await screen.findByTestId("logs-forbidden")).toBeTruthy();
    expect(screen.queryByTestId("logs-empty")).toBeNull();
    expect(screen.queryByTestId("logs-error")).toBeNull();
    expect(screen.queryByTestId("logs-pane")).toBeNull();
  });

  it("stops the spinner when the unit list itself is refused", async () => {
    respond([
      [/\/api\/logs\/units/, () => ({ status: 403, body: { detail: "operator role required" } })],
    ]);
    render(<LogsView role="owner" />);

    expect(await screen.findByTestId("logs-forbidden")).toBeTruthy();
    await waitFor(() => expect(screen.queryByTestId("logs-loading")).toBeNull());
    expect(screen.queryByTestId("logs-unit")).toBeNull();
    // Nothing to read, so nothing is asked for.
    expect(requests.filter((u) => /\/api\/logs\?/.test(u))).toHaveLength(0);
  });

  /**
   * `{"units": [], "available": true}` is what the bridge answers when
   * journalctl exists but no `robothor-*.service` is installed — a real state
   * on a dev checkout, and one that used to spin for ever.
   */
  it("says journald is here but no unit is installed, rather than spinning", async () => {
    respond([[/\/api\/logs\/units/, () => ({ body: { units: [], available: true } })]]);
    render(<LogsView role="owner" />);

    expect(await screen.findByTestId("logs-no-units")).toBeTruthy();
    await waitFor(() => expect(screen.queryByTestId("logs-loading")).toBeNull());
    expect(screen.queryByTestId("logs-unavailable")).toBeNull();
    expect(requests.filter((u) => /\/api\/logs\?/.test(u))).toHaveLength(0);
  });

  it("renders no journald as an honest empty state, never as an error", async () => {
    respond([[/\/api\/logs/, () => ({ body: NO_JOURNALD })]]);
    render(<LogsView role="owner" />);

    const state = await screen.findByTestId("logs-unavailable");
    expect(state.textContent).toContain("journalctl is not installed");
    expect(screen.queryByTestId("logs-error")).toBeNull();
    expect(screen.queryByTestId("logs-pane")).toBeNull();
    // Nothing to pick, so nothing is asked for.
    expect(requests.filter((u) => /\/api\/logs\?/.test(u))).toHaveLength(0);
  });

  it("renders an unavailable READ the same way, with the read's own reason", async () => {
    respond([
      [/\/api\/logs\/units/, () => ({ body: UNITS })],
      [
        /\/api\/logs\?/,
        () => ({
          body: {
            unit: "robothor-engine",
            available: false,
            reason: "timed out",
            lines: [],
            truncated: false,
          },
        }),
      ],
    ]);
    render(<LogsView role="owner" />);

    expect((await screen.findByTestId("logs-unavailable")).textContent).toContain("timed out");
    expect(screen.queryByTestId("logs-error")).toBeNull();
  });

  it("gives a line with no timestamp a placeholder rather than a collapsed gap", async () => {
    healthy();
    render(<LogsView role="owner" />);
    await screen.findByTestId("logs-pane");

    // The line journald gave no metadata for. Eight spaces collapse in HTML and
    // take the column with them, so the message jumps left out of alignment.
    const line = screen.getByTestId("logs-line-3");
    expect(line.textContent).toMatch(/--:--:--/);
  });

  it("keeps auto-refresh off until it is asked for, then beats every 10 seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    healthy();
    render(<LogsView role="owner" />);
    await waitFor(() => expect(requests.filter((u) => /\/api\/logs\?/.test(u)).length).toBe(1));

    await vi.advanceTimersByTimeAsync(60_000);
    expect(requests.filter((u) => /\/api\/logs\?/.test(u))).toHaveLength(1);

    fireEvent.click(screen.getByTestId("logs-auto"));
    expect(screen.getByTestId("logs-auto")).toHaveAttribute("aria-pressed", "true");
    await vi.advanceTimersByTimeAsync(10_000);
    await waitFor(() =>
      expect(requests.filter((u) => /\/api\/logs\?/.test(u)).length).toBeGreaterThan(1)
    );
  });

  it("stops the beat while the view is not on screen", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    healthy();
    const { rerender } = render(<LogsView role="owner" />);
    await waitFor(() => expect(requests.filter((u) => /\/api\/logs\?/.test(u)).length).toBe(1));
    fireEvent.click(screen.getByTestId("logs-auto"));
    await vi.advanceTimersByTimeAsync(10_000);
    const beating = requests.filter((u) => /\/api\/logs\?/.test(u)).length;
    expect(beating).toBeGreaterThan(1);

    rerender(<LogsView role="owner" visible={false} />);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(requests.filter((u) => /\/api\/logs\?/.test(u))).toHaveLength(beating);
  });

  it("tells a non-operator whose screen this is, and asks the bridge nothing", async () => {
    healthy();
    render(<LogsView role="auditor" />);

    // An auditor reads the record of decisions; the journal is every value
    // every process printed, which is a much wider surface.
    expect(await screen.findByTestId("logs-not-yours")).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not decide a role it has not been told yet", () => {
    healthy();
    render(<LogsView role={undefined} roleLoading />);
    expect(screen.queryByTestId("logs-not-yours")).toBeNull();
  });

  it("touches nothing while it is not the view on screen", () => {
    healthy();
    render(<LogsView role="owner" visible={false} />);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

/** The options of a native select. */
function optionsOf(select: HTMLElement): HTMLOptionsCollection {
  return (select as HTMLSelectElement).options;
}
