/**
 * Picking a period in a report workspace (C3): a stored edition shows at
 * once with no request; a missing one is asked for once, with a visible
 * "building" state until it arrives; a future period is refused before any
 * request; the daily on-demand cap is said plainly. Drawn inside the real
 * `RoomView` so "shows" means the pinned place.
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StreamEvent } from "@/lib/event-bus/use-event-stream";

let streamEvents: StreamEvent[] = [];
vi.mock("@/lib/event-bus/use-event-stream", () => ({
  useEventStream: () => ({ events: streamEvents, connected: true, synced: true }),
}));
vi.mock("@/components/chat-panel", () => ({ ChatPanel: () => <div data-testid="room-chat" /> }));
vi.mock("@/components/business/bar-chart", () => ({
  BarChart: ({ title }: { title: string }) => <div data-testid="bar-chart">{title}</div>,
}));
vi.mock("@/components/business/markdown-view", () => ({
  MarkdownView: ({ content }: { content: string }) => <div data-testid="markdown-view">{content}</div>,
}));

import { RoomView } from "@/components/rooms/room-view";
import { ROOM_CONTROL_SLOTS } from "@/components/rooms/room-controls";
import { PeriodSelector } from "@/components/rooms/period-selector";
import { EDITIONS, REPORT_ID, edition, reply, room } from "@/lib/rooms/__tests__/fixtures";
import type { Edition } from "@/lib/rooms/types";
import { mockBridge, slotCtx, withClient } from "./controls-harness";

let bridge: ReturnType<typeof mockBridge>;
let editions: Edition[];

function roomEvent(id: string, type: string): StreamEvent {
  return {
    id,
    stream: "agent",
    timestamp: "2026-11-21T00:00:00Z",
    type,
    source: "engine",
    actor: "main",
    payload: { room_id: REPORT_ID, tenant_id: "test-tenant" },
    correlation_id: "",
  };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["Date", "setTimeout", "setInterval", "clearTimeout", "clearInterval"] });
  vi.setSystemTime(new Date("2026-11-25T12:00:00Z"));
  streamEvents = [];
  editions = [...EDITIONS];
  bridge = mockBridge();
  bridge.on("GET", `/rooms/${REPORT_ID}`, room());
  bridge.on("GET", `/rooms/${REPORT_ID}/editions`, () => reply({ editions }));
  bridge.on("POST", `/rooms/${REPORT_ID}/read`, { ok: true });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function openView() {
  const utils = withClient(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} slots={ROOM_CONTROL_SLOTS} />);
  await screen.findByTestId("room-pinned-edition");
  return utils;
}

function pinnedTitle() {
  return within(screen.getByTestId("room-pinned-edition")).getByTestId("edition-title").textContent;
}

function pick(grain: "day" | "week" | "month" | "custom", dates: { anchor?: string; start?: string; end?: string }) {
  // The trigger toggles the picker; a refused pick leaves it open.
  if (!screen.queryByTestId("period-apply")) fireEvent.click(screen.getByTestId("period-trigger"));
  fireEvent.click(screen.getByTestId(`period-grain-${grain}`));
  if (dates.anchor) fireEvent.change(screen.getByTestId("period-anchor"), { target: { value: dates.anchor } });
  if (dates.start) fireEvent.change(screen.getByTestId("period-start"), { target: { value: dates.start } });
  if (dates.end) fireEvent.change(screen.getByTestId("period-end"), { target: { value: dates.end } });
  fireEvent.click(screen.getByTestId("period-apply"));
}

describe("period selector", () => {
  it("shows the stored edition for the period at once, with no request", async () => {
    await openView();
    expect(pinnedTitle()).toBe("Week 47");
    pick("week", { anchor: "2026-11-11" });
    expect(pinnedTitle()).toBe("Week 46");
    expect(bridge.calls("POST", `/rooms/${REPORT_ID}/periods`)).toHaveLength(0);
  });

  it("asks once for a missing period, shows it building, and shows it when it arrives", async () => {
    const built = edition({
      id: "e-day",
      seq: 3,
      grain: "day",
      title: "Wed 18 Nov",
      periodStart: "2026-11-18",
      periodEnd: "2026-11-18",
    });
    bridge.on("POST", `/rooms/${REPORT_ID}/periods`, () => reply({ ...built, status: "building" }));
    bridge.on("GET", `/rooms/${REPORT_ID}/editions/e-day`, () => reply({ ...built, status: "building" }));
    const { rerender } = await openView();
    pick("day", { anchor: "2026-11-18" });
    await waitFor(() => expect(bridge.calls("POST", `/rooms/${REPORT_ID}/periods`)).toHaveLength(1));
    expect(bridge.calls("POST", `/rooms/${REPORT_ID}/periods`)[0].body).toEqual({ grain: "day", anchor: "2026-11-18" });
    const status = await screen.findByTestId("period-status");
    expect(status).toHaveTextContent(/building/i);
    expect(status).toHaveAttribute("aria-live", "polite");

    // The engine publishes it; the room's event brings it in.
    editions = [built, ...EDITIONS];
    streamEvents = [roomEvent("ev-1", "room.edition")];
    rerender(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} slots={ROOM_CONTROL_SLOTS} />);
    await waitFor(() => expect(pinnedTitle()).toBe("Wed 18 Nov"));
    expect(screen.queryByTestId("period-status")).toBeNull();
  });

  it("falls back to polling when no event arrives", async () => {
    const built = edition({ id: "e-m", seq: 4, grain: "month", title: "October", periodStart: "2026-10-01", periodEnd: "2026-10-31" });
    let status: Edition["status"] = "building";
    bridge.on("POST", `/rooms/${REPORT_ID}/periods`, () => reply({ ...built, status: "building" }));
    bridge.on("GET", `/rooms/${REPORT_ID}/editions/e-m`, () => {
      if (status === "published") editions = [built, ...EDITIONS];
      return reply({ ...built, status });
    });
    await openView();
    pick("month", { anchor: "2026-10-15" });
    await screen.findByTestId("period-status");
    status = "published";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    await waitFor(() => expect(pinnedTitle()).toBe("October"));
  });

  it("sends a custom range with its end", async () => {
    bridge.on("POST", `/rooms/${REPORT_ID}/periods`, () =>
      reply(edition({ id: "e-c", grain: "custom", status: "building", periodStart: "2026-11-01", periodEnd: "2026-11-10" })),
    );
    bridge.on("GET", `/rooms/${REPORT_ID}/editions/e-c`, () => reply(edition({ id: "e-c", status: "building" })));
    await openView();
    pick("custom", { start: "2026-11-01", end: "2026-11-10" });
    await waitFor(() =>
      expect(bridge.calls("POST", `/rooms/${REPORT_ID}/periods`)[0]?.body).toEqual({
        grain: "custom",
        anchor: "2026-11-01",
        end: "2026-11-10",
      }),
    );
  });

  it("refuses a future period before any request", async () => {
    await openView();
    pick("day", { anchor: "2026-11-26" });
    expect(await screen.findByTestId("period-error")).toHaveTextContent(/hasn.t happened yet|future/i);
    pick("custom", { start: "2026-11-20", end: "2026-11-30" });
    expect(await screen.findByTestId("period-error")).toHaveTextContent(/hasn.t happened yet|future/i);
    expect(bridge.calls("POST", `/rooms/${REPORT_ID}/periods`)).toHaveLength(0);
  });

  it("refuses a custom range that runs backwards", async () => {
    await openView();
    pick("custom", { start: "2026-11-10", end: "2026-11-01" });
    expect(await screen.findByTestId("period-error")).toHaveTextContent(/end.*after|before/i);
    expect(bridge.calls("POST", `/rooms/${REPORT_ID}/periods`)).toHaveLength(0);
  });

  it("says the daily on-demand cap is reached and starts nothing", async () => {
    bridge.on("POST", `/rooms/${REPORT_ID}/periods`, () =>
      reply({ detail: { error: "on_demand_cap", message: "3 on-demand runs a day", limit: 3 } }, 429),
    );
    await openView();
    pick("day", { anchor: "2026-11-18" });
    const error = await screen.findByTestId("period-error");
    expect(error).toHaveTextContent(/today's limit/i);
    expect(error).toHaveTextContent("3");
    expect(screen.queryByTestId("period-status")).toBeNull();
    expect(pinnedTitle()).toBe("Week 47");
  });

  it("shows an edition the server already had (a shared request) at once", async () => {
    const existing = edition({ id: "e-had", seq: 5, grain: "day", title: "Tue 17 Nov", periodStart: "2026-11-17", periodEnd: "2026-11-17" });
    bridge.on("POST", `/rooms/${REPORT_ID}/periods`, () => {
      editions = [existing, ...EDITIONS];
      return reply(existing);
    });
    await openView();
    pick("day", { anchor: "2026-11-17" });
    await waitFor(() => expect(pinnedTitle()).toBe("Tue 17 Nov"));
    expect(screen.queryByTestId("period-status")).toBeNull();
  });

  it("is disabled for a user who may not request a period", () => {
    bridge.on("GET", "/noop", {});
    withClient(<PeriodSelector ctx={slotCtx({ access: "viewer" })} />);
    fireEvent.click(screen.getByTestId("period-trigger"));
    fireEvent.click(screen.getByTestId("period-grain-day"));
    fireEvent.change(screen.getByTestId("period-anchor"), { target: { value: "2026-11-18" } });
    fireEvent.click(screen.getByTestId("period-apply"));
    expect(screen.getByTestId("period-error")).toHaveTextContent(/no edition for this period/i);
    expect(bridge.calls("POST", `/rooms/${REPORT_ID}/periods`)).toHaveLength(0);
  });

  it("labels the period on show", () => {
    withClient(<PeriodSelector ctx={slotCtx()} />);
    expect(screen.getByTestId("period-trigger")).toHaveTextContent(/week/i);
    expect(screen.getByTestId("period-trigger")).toHaveTextContent("16 Nov – 22 Nov 2026");
  });
});
