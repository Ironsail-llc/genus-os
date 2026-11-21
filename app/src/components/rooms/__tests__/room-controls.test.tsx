/**
 * The workspace header's controls: what each access level is offered (a
 * read-only user gets none of the mutating ones), Run now, a preview run only
 * its requester sees, duplicate as mine, archive, an admin's restore, and the
 * drawers they open. Stories A2, A5, A6, A8, B6, F2, G6 (UI).
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StreamEvent } from "@/lib/event-bus/use-event-stream";

let streamEvents: StreamEvent[] = [];
vi.mock("@/lib/event-bus/use-event-stream", () => ({
  useEventStream: () => ({ events: streamEvents, connected: true, synced: true }),
}));
vi.mock("@/components/business/bar-chart", () => ({
  BarChart: ({ title }: { title: string }) => <div data-testid="bar-chart">{title}</div>,
}));
vi.mock("@/components/business/markdown-view", () => ({
  MarkdownView: ({ content }: { content: string }) => <div data-testid="markdown-view">{content}</div>,
}));

import { RoomHeaderActions, ROOM_CONTROL_SLOTS } from "@/components/rooms/room-controls";
import { ROOM_SLOTS_WITH_CONTROLS } from "@/components/rooms/room-controls-lazy";
import { AGENT_ID, REPORT_ID, edition, reply, room } from "@/lib/rooms/__tests__/fixtures";
import type { Access, Edition } from "@/lib/rooms/types";
import { mockBridge, slotCtx, withClient } from "./controls-harness";

let bridge: ReturnType<typeof mockBridge>;

beforeEach(() => {
  streamEvents = [];
  bridge = mockBridge();
  bridge.on("GET", `/rooms/${REPORT_ID}/revisions`, { revisions: [] });
  bridge.on("GET", `/rooms/${REPORT_ID}/notify`, { inApp: true, muted: false, channels: {}, linkedChannels: [] });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function header(access: Access, overrides = {}) {
  const ctx = slotCtx({ access, ...overrides });
  const utils = withClient(<RoomHeaderActions ctx={ctx} />);
  return { ctx, ...utils };
}

function openMenu() {
  fireEvent.click(screen.getByTestId("room-more"));
  return screen.getByRole("menu");
}

describe("header actions by access (G6)", () => {
  it("offers an assigned user run, preview, settings, share, duplicate and archive", () => {
    header("assigned");
    expect(screen.getByTestId("room-run-now")).toBeEnabled();
    expect(screen.getByTestId("room-preview-run")).toBeEnabled();
    expect(screen.getByTestId("room-settings-open")).toBeInTheDocument();
    expect(screen.getByTestId("room-share-open")).toBeInTheDocument();
    const menu = openMenu();
    expect(within(menu).getByTestId("room-menu-duplicate")).toBeInTheDocument();
    expect(within(menu).getByTestId("room-menu-archive")).toBeInTheDocument();
  });

  it("offers a shared user run and duplicate, settings to read, and no share or archive", () => {
    header("shared");
    expect(screen.getByTestId("room-run-now")).toBeInTheDocument();
    expect(screen.getByTestId("room-settings-open")).toBeInTheDocument();
    expect(screen.queryByTestId("room-share-open")).toBeNull();
    const menu = openMenu();
    expect(within(menu).getByTestId("room-menu-duplicate")).toBeInTheDocument();
    expect(within(menu).queryByTestId("room-menu-archive")).toBeNull();
  });

  it("offers a viewer none of the mutating controls", () => {
    header("viewer");
    for (const id of ["room-run-now", "room-preview-run", "room-settings-open", "room-share-open"]) {
      expect(screen.queryByTestId(id)).toBeNull();
    }
    const menu = openMenu();
    expect(within(menu).queryByTestId("room-menu-duplicate")).toBeNull();
    expect(within(menu).queryByTestId("room-menu-archive")).toBeNull();
    // What is personal and read-only stays: notification choices and provenance.
    expect(within(menu).getByTestId("room-menu-notify")).toBeInTheDocument();
    expect(within(menu).getByTestId("room-menu-provenance")).toBeInTheDocument();
  });

  it("gives an agent workspace rename (settings), share and archive, but no runs (F2)", () => {
    header("assigned", { id: AGENT_ID, kind: "agent", runsAs: undefined });
    expect(screen.queryByTestId("room-run-now")).toBeNull();
    expect(screen.queryByTestId("room-preview-run")).toBeNull();
    expect(screen.getByTestId("room-settings-open")).toBeInTheDocument();
    expect(screen.getByTestId("room-share-open")).toBeInTheDocument();
    expect(within(openMenu()).getByTestId("room-menu-archive")).toBeInTheDocument();
  });

  it("on an archived workspace offers duplicate, and restore only to an admin (A6)", () => {
    header("assigned", { archived: true });
    expect(screen.queryByTestId("room-run-now")).toBeNull();
    expect(screen.queryByTestId("room-share-open")).toBeNull();
    const menu = openMenu();
    expect(within(menu).getByTestId("room-menu-duplicate")).toBeInTheDocument();
    expect(within(menu).queryByTestId("room-menu-archive")).toBeNull();
    expect(within(menu).queryByTestId("room-menu-unarchive")).toBeNull();
  });

  it("lets an admin restore an archived workspace", async () => {
    bridge.on("POST", `/rooms/${REPORT_ID}/unarchive`, room({ archived: false, paused: true }));
    const { ctx } = header("admin", { archived: true });
    fireEvent.click(within(openMenu()).getByTestId("room-menu-unarchive"));
    await waitFor(() => expect(bridge.calls("POST", `/rooms/${REPORT_ID}/unarchive`)).toHaveLength(1));
    await waitFor(() => expect(ctx.refresh).toHaveBeenCalled());
  });
});

describe("run now (B6, A5)", () => {
  it("starts a run, shows it running, and shows the edition when it is published", async () => {
    let status: Edition["status"] = "building";
    bridge.on("POST", `/rooms/${REPORT_ID}/run`, () => reply(edition({ id: "e-run", seq: 3, status: "building" }), 202));
    bridge.on("GET", `/rooms/${REPORT_ID}/editions/e-run`, () => reply(edition({ id: "e-run", seq: 3, status })));
    const { ctx, rerender } = header("shared");
    fireEvent.click(screen.getByTestId("room-run-now"));
    await waitFor(() => expect(bridge.calls("POST", `/rooms/${REPORT_ID}/run`)).toHaveLength(1));
    expect(bridge.calls("POST", `/rooms/${REPORT_ID}/run`)[0].body).toEqual({});
    expect(await screen.findByTestId("room-run-status")).toHaveTextContent(/running/i);
    expect(screen.getByTestId("room-run-now")).toBeDisabled();

    status = "published";
    const published = edition({ id: "e-run", seq: 3, status: "published" });
    const next = { ...ctx, editions: [published, ...ctx.editions] };
    rerender(<RoomHeaderActions ctx={next} />);
    await waitFor(() => expect(next.showEdition).toHaveBeenCalledWith(published));
    expect(screen.queryByTestId("room-run-status")).toBeNull();
  });

  it("still runs a paused report", async () => {
    bridge.on("POST", `/rooms/${REPORT_ID}/run`, () => reply(edition({ id: "e-run", status: "building" })));
    header("assigned", { paused: true });
    fireEvent.click(screen.getByTestId("room-run-now"));
    await waitFor(() => expect(bridge.calls("POST", `/rooms/${REPORT_ID}/run`)).toHaveLength(1));
  });

  it("says plainly when the run is refused", async () => {
    bridge.on("POST", `/rooms/${REPORT_ID}/run`, () => reply({ detail: { error: "on_demand_cap", limit: 3 } }, 429));
    header("shared");
    fireEvent.click(screen.getByTestId("room-run-now"));
    expect(await screen.findByTestId("room-run-error")).toHaveTextContent(/limit/i);
  });
});

describe("preview run (A2)", () => {
  it("is labelled a dry run nobody else sees, and opens the preview for its requester only", async () => {
    let status: Edition["status"] = "building";
    bridge.on("POST", `/rooms/${REPORT_ID}/run`, () =>
      reply(edition({ id: "e-prev", seq: 9, preview: true, status: "building", title: "Week 47 (preview)" })),
    );
    bridge.on("GET", `/rooms/${REPORT_ID}/editions/e-prev`, () =>
      reply(edition({ id: "e-prev", seq: 9, preview: true, status, title: "Week 47 (preview)" })),
    );
    const { ctx } = header("assigned");
    const button = screen.getByTestId("room-preview-run");
    expect(button).toHaveAccessibleDescription(/only you/i);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(button);
    await waitFor(() => expect(bridge.calls("POST", `/rooms/${REPORT_ID}/run`)).toHaveLength(1));
    expect(bridge.calls("POST", `/rooms/${REPORT_ID}/run`)[0].body).toEqual({ preview: true });

    const panel = await screen.findByTestId("room-preview");
    expect(panel).toHaveTextContent(/only you can see/i);
    expect(panel).toHaveTextContent(/building/i);

    status = "published";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(await within(panel).findByTestId("edition-title")).toHaveTextContent("Week 47 (preview)");
    expect(within(panel).getByTestId("room-preview-badge")).toHaveTextContent("Preview");
    // Never put in the shared, pinned place.
    expect(ctx.showEdition).not.toHaveBeenCalled();
  });

  it("shows its requester why a preview failed", async () => {
    bridge.on("POST", `/rooms/${REPORT_ID}/run`, () => reply(edition({ id: "e-prev", preview: true, status: "building" })));
    bridge.on("GET", `/rooms/${REPORT_ID}/editions/e-prev`, () =>
      reply({ ...edition({ id: "e-prev", preview: true, status: "failed" }), error: "The sales tool timed out" }),
    );
    vi.useFakeTimers({ shouldAdvanceTime: true });
    header("assigned");
    fireEvent.click(screen.getByTestId("room-preview-run"));
    const panel = await screen.findByTestId("room-preview");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(await within(panel).findByTestId("room-preview-failed")).toHaveTextContent("The sales tool timed out");
  });
});

describe("duplicate and archive (A8, A6)", () => {
  it("duplicates as mine and opens the copy", async () => {
    const COPY = "66666666-6666-4666-8666-666666666666";
    bridge.on("POST", `/rooms/${REPORT_ID}/duplicate`, room({ id: COPY, title: "Weekly sales (copy)" }));
    const push = vi.spyOn(window.history, "pushState");
    header("shared");
    fireEvent.click(within(openMenu()).getByTestId("room-menu-duplicate"));
    await waitFor(() => expect(bridge.calls("POST", `/rooms/${REPORT_ID}/duplicate`)).toHaveLength(1));
    await waitFor(() => expect(push).toHaveBeenCalledWith(null, "", `/?v=chat&room=${COPY}`));
    push.mockRestore();
  });

  it("archives only after a confirmation that says what is kept", async () => {
    bridge.on("POST", `/rooms/${REPORT_ID}/archive`, room({ archived: true }));
    const { ctx } = header("assigned");
    fireEvent.click(within(openMenu()).getByTestId("room-menu-archive"));
    const confirm = await screen.findByTestId("room-archive-confirm");
    expect(confirm).toHaveTextContent(/history, editions and tasks are kept/i);
    expect(bridge.calls("POST", `/rooms/${REPORT_ID}/archive`)).toHaveLength(0);
    fireEvent.click(within(confirm).getByRole("button", { name: /^archive$/i }));
    await waitFor(() => expect(bridge.calls("POST", `/rooms/${REPORT_ID}/archive`)).toHaveLength(1));
    await waitFor(() => expect(ctx.refresh).toHaveBeenCalled());
  });
});

describe("drawers open from the header", () => {
  it("opens settings, share and provenance", async () => {
    header("assigned");
    fireEvent.click(screen.getByTestId("room-settings-open"));
    expect(await screen.findByTestId("room-settings")).toBeInTheDocument();
  });

  it("opens the share dialog", async () => {
    bridge.on("GET", "/users", { users: [] });
    header("assigned");
    fireEvent.click(screen.getByTestId("room-share-open"));
    expect(await screen.findByTestId("share-dialog")).toBeInTheDocument();
  });

  it("opens provenance for the edition on show", async () => {
    header("viewer");
    fireEvent.click(within(openMenu()).getByTestId("room-menu-provenance"));
    expect(await screen.findByTestId("provenance-drawer")).toHaveTextContent("run-1");
  });
});

describe("slots", () => {
  it("fills the header actions and the period selector", () => {
    expect(typeof ROOM_CONTROL_SLOTS.headerActions).toBe("function");
    expect(typeof ROOM_CONTROL_SLOTS.periodSelector).toBe("function");
  });

  it("the shell's lazily loaded slots draw the same controls", async () => {
    const ctx = slotCtx({ access: "assigned" });
    withClient(
      <>
        {ROOM_SLOTS_WITH_CONTROLS.headerActions!(ctx)}
        {ROOM_SLOTS_WITH_CONTROLS.periodSelector!(ctx)}
      </>,
    );
    expect(await screen.findByTestId("room-run-now")).toBeInTheDocument();
    expect(await screen.findByTestId("period-trigger")).toBeInTheDocument();
  });
});
