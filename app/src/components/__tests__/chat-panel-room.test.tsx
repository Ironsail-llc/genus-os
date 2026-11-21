/**
 * The chat panel pointed at a room.
 *
 * It names the room by id on every request and never an agent or a session
 * key; it shows who wrote each message, before and after a reload; a send
 * while the room's agent is answering somebody else is queued, says whom it is
 * answering, and goes out later; a viewer reads but cannot post; and live room
 * messages appear without a refresh. With no room, nothing changes.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ notifyConversationUpdate: vi.fn(), setRender: vi.fn() }),
}));
vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (value: string) => value }));
vi.mock("@/components/business/bar-chart", () => ({
  BarChart: ({ title }: { title: string }) => <div data-testid="bar-chart">{title}</div>,
}));
vi.mock("@/components/business/markdown-view", () => ({
  MarkdownView: ({ content }: { content: string }) => <div data-testid="markdown-view">{content}</div>,
}));

import { ChatPanel } from "../chat-panel";
import { EDITION_2, REPORT_ID, edition, reply } from "@/lib/rooms/__tests__/fixtures";

function sseResponse(events: Array<{ event: string; data: unknown }>) {
  const body = events.map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join("");
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

const HISTORY = [
  { id: 10, role: "user", content: "How did we do?", authorUserId: "u-bob", authorName: "Bob" },
  { id: 11, role: "assistant", content: "Week 47 is out.", authorName: "Analyst", editionId: EDITION_2 },
];

let history: unknown[];
let sendReplies: Array<() => Response>;
let fetchMock: ReturnType<typeof vi.fn>;
const sends: Array<Record<string, unknown>> = [];

beforeEach(() => {
  history = [...HISTORY];
  sendReplies = [];
  sends.length = 0;
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/chat/history")) return reply({ messages: history, hasMore: url.includes("before=") ? false : true });
    if (url === "/api/chat/send") {
      sends.push(JSON.parse(String(init?.body ?? "{}")));
      const next = sendReplies.shift();
      return next ? next() : sseResponse([{ event: "done", data: { text: "Noted." } }]);
    }
    if (url === `/api/bridge/api/rooms/${REPORT_ID}/editions/${EDITION_2}`) return reply(edition());
    return reply({ messages: [], agents: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

function urls() {
  return fetchMock.mock.calls.map(([url]) => String(url));
}

async function type(text: string) {
  const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
  await act(async () => {
    fireEvent.change(input, { target: { value: text } });
    fireEvent.keyDown(input, { key: "Enter" });
  });
}

describe("ChatPanel in a room", () => {
  it("reads the room's history by room id, and asks nothing about agents, plans or deep runs", async () => {
    render(<ChatPanel variant="page" room={{ id: REPORT_ID }} />);
    await screen.findByText("How did we do?");
    expect(urls()).toContain(`/api/chat/history?room=${REPORT_ID}`);
    expect(urls().some((u) => u.includes("agent-manifests"))).toBe(false);
    expect(urls().some((u) => u.includes("/plan/status") || u.includes("/deep/status"))).toBe(false);
    expect(screen.queryByTestId("agent-switcher")).toBeNull();
    expect(screen.queryByTestId("plan-toggle")).toBeNull();
  });

  it("sends to the room by id, never naming an agent", async () => {
    render(<ChatPanel variant="page" room={{ id: REPORT_ID }} />);
    await screen.findByText("How did we do?");
    await type("And margins?");
    await screen.findByText("Noted.");
    expect(sends[0]).toEqual({ message: "And margins?", request_id: expect.any(String), room: REPORT_ID });
  });

  it("shows each message's author, and the edition a message announced, from history", async () => {
    render(<ChatPanel variant="page" room={{ id: REPORT_ID }} />);
    await screen.findByText("How did we do?");
    const authors = screen.getAllByTestId("message-author").map((el) => el.textContent);
    expect(authors).toEqual(["Bob", "Analyst"]);
    expect(await screen.findByTestId("bar-chart")).toHaveTextContent("Revenue by day");
  });

  it("labels the caller's own new message as theirs", async () => {
    history = [];
    render(<ChatPanel variant="page" room={{ id: REPORT_ID }} />);
    // The empty room's history has answered: what is typed now is not replaced by it.
    await screen.findByTestId("room-chat-empty");
    await type("First!");
    await screen.findByText("Noted.");
    const mine = screen.getAllByTestId("message-row")[0];
    expect(within(mine).getByTestId("message-author")).toHaveTextContent("You");
  });

  it("pages back through a room's older messages by room id", async () => {
    render(<ChatPanel variant="page" room={{ id: REPORT_ID }} />);
    fireEvent.click(await screen.findByTestId("load-earlier"));
    await waitFor(() => expect(urls()).toContain(`/api/chat/history?room=${REPORT_ID}&before=10&limit=50`));
  });

  it("queues a message while the room is busy, says whom it is answering, and sends it later", async () => {
    sendReplies.push(() => reply({ error: "room_busy", answering: "Alice", request_admitted: false }, 409));
    const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByText("How did we do?");
    await type("Me too?");
    await waitFor(() => expect(screen.getByTestId("room-busy-notice")).toHaveTextContent("answering Alice…"));
    // The message stays where it was typed, marked queued; nothing is lost.
    expect(screen.getByText("Me too?")).toBeInTheDocument();
    expect(screen.getByTestId("message-note")).toHaveTextContent(/answering Alice/);
    expect(screen.queryByText("room_busy")).toBeNull();

    // The room moved on (a message landed): the queued one goes out, once.
    await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
    await screen.findByText("Noted.");
    expect(sends.map((s) => s.message)).toEqual(["Me too?", "Me too?"]);
    expect(screen.getAllByText("Me too?")).toHaveLength(1);
    expect(screen.queryByTestId("room-busy-notice")).toBeNull();
  });

  it("keeps typing order behind a busy room: a new message waits behind the queued one", async () => {
    history = [];
    sendReplies.push(() => reply({ error: "room_busy", answering: "Alice", request_admitted: false }, 409));
    const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByTestId("room-chat-empty");
    await type("first");
    await screen.findByTestId("room-busy-notice");
    await type("second");
    // Not sent ahead of the one already waiting.
    expect(sends.map((s) => s.message)).toEqual(["first"]);
    expect(screen.getAllByTestId("message-note").map((n) => n.textContent)).toEqual([
      "Queued — answering Alice…",
      "Queued — answering Alice…",
    ]);

    await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
    // One message per turn, in the order they were typed.
    await waitFor(() => expect(sends.map((s) => s.message)).toEqual(["first", "first", "second"]));
    await waitFor(() => expect(screen.getAllByText("Noted.")).toHaveLength(2));
    expect(screen.queryByTestId("room-busy-notice")).toBeNull();

    // Each queued message is its own stored row, so catching up shows no duplicate.
    history = [
      { id: 30, role: "user", content: "first", authorName: "Alice" },
      { id: 31, role: "assistant", content: "Noted.", authorName: "Analyst" },
      { id: 32, role: "user", content: "second", authorName: "Alice" },
      { id: 33, role: "assistant", content: "Noted.", authorName: "Analyst" },
    ];
    await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 2 }} />));
    await waitFor(() => expect(screen.getAllByTestId("message-author")[0]).toHaveTextContent("Alice"));
    expect(screen.getAllByText("first")).toHaveLength(1);
    expect(screen.getAllByText("second")).toHaveLength(1);
    expect(screen.getAllByText("Noted.")).toHaveLength(2);
  });

  it("keeps a queued message when the workspace is left, and sends it on return", async () => {
    history = [];
    sendReplies.push(() => reply({ error: "room_busy", answering: "Alice", request_admitted: false }, 409));
    const first = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByTestId("room-chat-empty");
    await type("hold this");
    await screen.findByTestId("room-busy-notice");
    first.unmount();

    const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    expect(await screen.findByText("hold this")).toBeInTheDocument();
    expect(screen.getByTestId("message-note")).toHaveTextContent(/Queued/);
    expect(screen.getByTestId("room-busy-notice")).toBeInTheDocument();
    await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
    await screen.findByText("Noted.");
    expect(sends.map((s) => s.message)).toEqual(["hold this", "hold this"]);
    expect(screen.getAllByText("hold this")).toHaveLength(1);
  });

  it("says the conversation could not be loaded when the room's history is refused", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) =>
      String(input).startsWith("/api/chat/history")
        ? reply({ error: "the engine did not confirm the workspace", messages: [] }, 502)
        : reply({}),
    );
    render(<ChatPanel variant="page" room={{ id: REPORT_ID }} />);
    expect(await screen.findByTestId("room-chat-unavailable")).toBeInTheDocument();
    expect(screen.queryByTestId("room-chat-empty")).toBeNull();
  });

  it("gives a viewer a disabled composer and says why", async () => {
    render(<ChatPanel variant="page" room={{ id: REPORT_ID, readOnly: true }} />);
    await screen.findByText("How did we do?");
    expect(screen.getByTestId("chat-input")).toBeDisabled();
    expect(screen.getByTestId("send-button")).toBeDisabled();
    expect(screen.getByTestId("room-readonly-notice")).toHaveTextContent(/view-only/i);
  });

  it("catches up on live messages without duplicating what is on screen", async () => {
    const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByText("How did we do?");
    history = [...HISTORY, { id: 12, role: "user", content: "Bob here again", authorName: "Bob" }];
    await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
    await screen.findByText("Bob here again");
    expect(screen.getAllByText("How did we do?")).toHaveLength(1);
  });

  it("adopts the stored copy of the caller's own message rather than showing it twice", async () => {
    history = [];
    const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByTestId("room-chat-empty");
    await type("Mine");
    await screen.findByText("Noted.");
    history = [
      { id: 20, role: "user", content: "Mine", authorName: "Alice" },
      { id: 21, role: "assistant", content: "Noted.", authorName: "Analyst" },
    ];
    await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
    await waitFor(() => expect(screen.getAllByTestId("message-author").map((el) => el.textContent)).toEqual(["Alice", "Analyst"]));
    expect(screen.getAllByText("Mine")).toHaveLength(1);
    expect(screen.getAllByText("Noted.")).toHaveLength(1);
  });
});

describe("ChatPanel with no room, exactly as before", () => {
  it("reads the main history, asks for the fleet listing, and sends no room", async () => {
    history = [{ id: 1, role: "user", content: "hello", authorName: "Bob" }];
    render(<ChatPanel variant="page" />);
    await screen.findByText("hello");
    expect(urls()).toContain("/api/chat/history");
    expect(urls()).toContain("/api/bridge/api/agent-manifests");
    expect(urls()).toContain("/api/chat/plan/status");
    expect(screen.queryByTestId("message-author")).toBeNull();
    await type("hi");
    await screen.findByText("Noted.");
    expect(sends[0]).toEqual({ message: "hi", request_id: expect.any(String) });
  });
});
