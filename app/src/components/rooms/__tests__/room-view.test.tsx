/**
 * Opening a workspace: its header, the latest published edition pinned as the
 * dashboard, the past editions, and the conversation (the chat panel pointed
 * at the room). Live room events bring new editions and messages in without a
 * refresh. A room the caller cannot see is a calm dead end with a way home.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StreamEvent } from "@/lib/event-bus/use-event-stream";
import {
  AGENT_ID,
  EDITIONS,
  EDITION_1,
  EMPTY_REPORT_ID,
  REPORT_ID,
  edition,
  reply,
  room,
} from "@/lib/rooms/__tests__/fixtures";
import type { Edition, Room } from "@/lib/rooms/types";

let streamEvents: StreamEvent[] = [];
vi.mock("@/lib/event-bus/use-event-stream", () => ({
  useEventStream: () => ({ events: streamEvents, connected: true, synced: true }),
}));

vi.mock("@/components/chat-panel", () => ({
  ChatPanel: (props: { room?: { id: string; readOnly?: boolean; readOnlyReason?: string; refreshKey?: number } }) => (
    <div
      data-testid="room-chat"
      data-room={props.room?.id}
      data-readonly={String(Boolean(props.room?.readOnly))}
      data-readonly-reason={props.room?.readOnlyReason ?? ""}
      data-refresh={String(props.room?.refreshKey ?? 0)}
    />
  ),
}));
vi.mock("@/components/business/bar-chart", () => ({
  BarChart: ({ title }: { title: string }) => <div data-testid="bar-chart">{title}</div>,
}));
vi.mock("@/components/business/markdown-view", () => ({
  MarkdownView: ({ content }: { content: string }) => <div data-testid="markdown-view">{content}</div>,
}));

import { RoomView } from "@/components/rooms/room-view";

let rooms: Record<string, Room>;
let editions: Record<string, Edition[]>;
let fetchMock: ReturnType<typeof vi.fn>;

function roomEvent(id: string, type: string, roomId: string): StreamEvent {
  return {
    id,
    stream: "agent",
    timestamp: "2026-11-21T00:00:00Z",
    type,
    source: "engine",
    actor: "main",
    payload: { room_id: roomId, tenant_id: "test-tenant" },
    correlation_id: "",
  };
}

beforeEach(() => {
  streamEvents = [];
  rooms = {
    [REPORT_ID]: room(),
    [AGENT_ID]: room({ id: AGENT_ID, kind: "agent", title: "Research desk", runsAs: undefined, nextRunAt: undefined }),
    [EMPTY_REPORT_ID]: room({ id: EMPTY_REPORT_ID, title: "Monthly margins" }),
  };
  editions = { [REPORT_ID]: [...EDITIONS], [AGENT_ID]: [], [EMPTY_REPORT_ID]: [] };
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const m = url.match(/^\/api\/bridge\/api\/rooms\/([^/]+)(\/.*)?$/);
    if (!m) return reply({ error: "unexpected" }, 500);
    const [, id, rest = ""] = m;
    if (rest === "/read" && init?.method === "POST") return reply({ ok: true });
    if (!rooms[id]) return reply({ error: "Not found" }, 404);
    if (rest === "") return reply(rooms[id]);
    if (rest === "/editions") return reply({ editions: editions[id] });
    return reply({ error: "unexpected" }, 500);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => vi.unstubAllGlobals());

function view(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
  return {
    ...utils,
    rerenderView: (next: ReactNode) => utils.rerender(<QueryClientProvider client={client}>{next}</QueryClientProvider>),
  };
}

function calls(path: string, method?: string) {
  return fetchMock.mock.calls.filter(
    ([url, init]) => String(url) === `/api/bridge/api/rooms${path}` && (method ? init?.method === method : !init?.method),
  ).length;
}

describe("RoomView", () => {
  it("shows the header: title, kind, members and who a report runs as", async () => {
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    const header = await screen.findByTestId("room-header");
    expect(within(header).getByRole("heading", { level: 1 })).toHaveTextContent("Weekly sales");
    expect(within(header).getByTestId("room-kind")).toHaveTextContent("Report");
    expect(within(header).getByTestId("room-members")).toHaveTextContent(/Alice.*Bob/);
    expect(within(header).getByTestId("room-runs-as")).toHaveTextContent("Runs as Alice");
    expect(within(header).queryByTestId("room-paused")).toBeNull();
  });

  it("shows a paused badge", async () => {
    rooms[REPORT_ID] = room({ paused: true });
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    expect(await screen.findByTestId("room-paused")).toHaveTextContent("Paused");
  });

  it("pins the latest published edition and lists the past ones", async () => {
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    const pinned = await screen.findByTestId("room-pinned-edition");
    expect(within(pinned).getByTestId("edition-title")).toHaveTextContent("Week 47");
    expect(await within(pinned).findByTestId("bar-chart")).toBeInTheDocument();
    const past = screen.getByTestId("room-past-editions");
    expect(within(past).getByText("Week 46")).toBeInTheDocument();
    expect(within(past).queryByText("Week 47")).toBeNull();
  });

  it("opens a past edition in the pinned place and comes back to the latest", async () => {
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    fireEvent.click(await screen.findByTestId(`room-past-edition-${EDITION_1}`));
    const pinned = screen.getByTestId("room-pinned-edition");
    expect(within(pinned).getByTestId("edition-title")).toHaveTextContent("Week 46");
    fireEvent.click(within(pinned).getByTestId("room-edition-latest"));
    expect(within(screen.getByTestId("room-pinned-edition")).getByTestId("edition-title")).toHaveTextContent("Week 47");
  });

  it("never pins a preview, a failed or a building edition", async () => {
    editions[REPORT_ID] = [
      edition({ id: "e-p", seq: 5, title: "Preview", preview: true }),
      edition({ id: "e-f", seq: 4, title: "Failed", status: "failed" }),
      edition({ id: "e-b", seq: 3, title: "Building", status: "building" }),
      ...EDITIONS,
    ];
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    const pinned = await screen.findByTestId("room-pinned-edition");
    expect(within(pinned).getByTestId("edition-title")).toHaveTextContent("Week 47");
    expect(screen.queryByText("Failed")).toBeNull();
  });

  it("says when the first edition of a report will arrive", async () => {
    view(<RoomView roomId={EMPTY_REPORT_ID} onLeave={vi.fn()} />);
    const empty = await screen.findByTestId("room-no-edition");
    expect(empty).toHaveTextContent(/first edition arrives/i);
    expect(empty).toHaveTextContent(/23 Nov/);
  });

  it("says a paused report will publish nothing until it is resumed", async () => {
    rooms[EMPTY_REPORT_ID] = room({ id: EMPTY_REPORT_ID, paused: true });
    view(<RoomView roomId={EMPTY_REPORT_ID} onLeave={vi.fn()} />);
    expect(await screen.findByTestId("room-no-edition")).toHaveTextContent(/paused/i);
  });

  it("gives an agent room no edition pane and no period controls", async () => {
    const periodSelector = vi.fn(() => <div data-testid="period-slot" />);
    view(<RoomView roomId={AGENT_ID} onLeave={vi.fn()} slots={{ periodSelector }} />);
    await screen.findByTestId("room-header");
    expect(screen.queryByTestId("room-editions")).toBeNull();
    expect(screen.queryByTestId("room-no-edition")).toBeNull();
    expect(screen.queryByTestId("period-slot")).toBeNull();
    expect(screen.queryByTestId("room-runs-as")).toBeNull();
  });

  it("hands lane slots the room and what this user may do", async () => {
    rooms[REPORT_ID] = room({ access: "viewer" });
    const headerActions = vi.fn(() => <div data-testid="actions-slot" />);
    const periodSelector = vi.fn(() => <div data-testid="period-slot" />);
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} slots={{ headerActions, periodSelector }} />);
    expect(await screen.findByTestId("actions-slot")).toBeInTheDocument();
    expect(await screen.findByTestId("period-slot")).toBeInTheDocument();
    const ctx = (headerActions.mock.calls.at(-1) as unknown as [{ room: Room; can: (a: string) => boolean }])[0];
    expect(ctx.room.id).toBe(REPORT_ID);
    expect(ctx.can("read")).toBe(true);
    expect(ctx.can("run")).toBe(false);
  });

  it("points the conversation at the room", async () => {
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    const chat = await screen.findByTestId("room-chat");
    expect(chat).toHaveAttribute("data-room", REPORT_ID);
    expect(chat).toHaveAttribute("data-readonly", "false");
  });

  it("makes the conversation read-only for a viewer", async () => {
    rooms[REPORT_ID] = room({ access: "viewer" });
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    expect(await screen.findByTestId("room-chat")).toHaveAttribute("data-readonly", "true");
  });

  it("shuts the conversation of an archived room, and tells the slots what is still allowed", async () => {
    rooms[REPORT_ID] = room({ access: "shared", archived: true });
    const headerActions = vi.fn(() => <div data-testid="actions-slot" />);
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} slots={{ headerActions }} />);
    const chat = await screen.findByTestId("room-chat");
    expect(chat).toHaveAttribute("data-readonly", "true");
    expect(chat).toHaveAttribute("data-readonly-reason", "archived");
    const ctx = (headerActions.mock.calls.at(-1) as unknown as [{ can: (a: string) => boolean }])[0];
    expect(ctx.can("run")).toBe(false);
    expect(ctx.can("post")).toBe(false);
    expect(ctx.can("read")).toBe(true);
    expect(ctx.can("duplicate")).toBe(true);
  });

  it("names view-only access as the reason a viewer cannot post", async () => {
    rooms[REPORT_ID] = room({ access: "viewer" });
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    expect(await screen.findByTestId("room-chat")).toHaveAttribute("data-readonly-reason", "viewer");
  });

  it("marks the room read when it opens", async () => {
    const markRead = vi.fn(async () => {});
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} markRead={markRead} />);
    await waitFor(() => expect(markRead).toHaveBeenCalledWith(REPORT_ID));
  });

  it("posts the read itself when no one hands it a markRead", async () => {
    view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    await waitFor(() => expect(calls(`/${REPORT_ID}/read`, "POST")).toBe(1));
  });

  it("shows a calm unavailable state for a room it cannot see, with a way back", async () => {
    const onLeave = vi.fn();
    const markRead = vi.fn(async () => {});
    view(<RoomView roomId="99999999-9999-4999-8999-999999999999" onLeave={onLeave} markRead={markRead} />);
    const gone = await screen.findByTestId("room-unavailable");
    expect(gone).toHaveTextContent("This workspace isn't available");
    const back = within(gone).getByRole("link", { name: /main conversation/i });
    expect(back).toHaveAttribute("href", "/?v=chat");
    fireEvent.click(back);
    expect(onLeave).toHaveBeenCalled();
    expect(screen.queryByTestId("room-chat")).toBeNull();
    expect(markRead).not.toHaveBeenCalled();
  });

  it("brings a new edition in live, and marks it read", async () => {
    const markRead = vi.fn(async () => {});
    const { rerenderView } = view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} markRead={markRead} />);
    await screen.findByTestId("room-pinned-edition");
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(1));
    editions[REPORT_ID] = [edition({ id: "e-3", seq: 3, title: "Week 48" }), ...EDITIONS];
    streamEvents = [roomEvent("9-0", "room.edition", REPORT_ID)];
    await act(async () => rerenderView(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} markRead={markRead} />));
    await waitFor(() =>
      expect(within(screen.getByTestId("room-pinned-edition")).getByTestId("edition-title")).toHaveTextContent("Week 48"),
    );
    expect(within(screen.getByTestId("room-past-editions")).getByText("Week 47")).toBeInTheDocument();
    expect(markRead).toHaveBeenCalledTimes(2);
  });

  it("tells the conversation to catch up on a live message", async () => {
    const { rerenderView } = view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    const chat = await screen.findByTestId("room-chat");
    expect(chat).toHaveAttribute("data-refresh", "0");
    streamEvents = [roomEvent("9-1", "room.message", REPORT_ID)];
    await act(async () => rerenderView(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />));
    expect(screen.getByTestId("room-chat")).toHaveAttribute("data-refresh", "1");
  });

  it("ignores another room's events", async () => {
    const { rerenderView } = view(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />);
    await screen.findByTestId("room-pinned-edition");
    const before = calls(`/${REPORT_ID}/editions`);
    streamEvents = [roomEvent("9-2", "room.edition", AGENT_ID), roomEvent("9-3", "room.message", AGENT_ID)];
    await act(async () => rerenderView(<RoomView roomId={REPORT_ID} onLeave={vi.fn()} />));
    expect(calls(`/${REPORT_ID}/editions`)).toBe(before);
    expect(screen.getByTestId("room-chat")).toHaveAttribute("data-refresh", "0");
  });
});
