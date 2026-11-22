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

const visual = vi.hoisted(() => ({ notifyConversationUpdate: vi.fn(), setRender: vi.fn() }));
vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => visual,
}));
vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (value: string) => value }));
vi.mock("@/components/business/bar-chart", () => ({
  BarChart: ({ title }: { title: string }) => <div data-testid="bar-chart">{title}</div>,
}));
vi.mock("@/components/business/markdown-view", () => ({
  MarkdownView: ({ content }: { content: string }) => <div data-testid="markdown-view">{content}</div>,
}));

import { ChatPanel } from "../chat-panel";
import { forgetRoomOutboxes } from "@/lib/chat/room-outbox";
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
let sendReplies: Array<() => Response | Promise<Response>>;
let fetchMock: ReturnType<typeof vi.fn>;
const sends: Array<Record<string, unknown>> = [];

beforeEach(() => {
  // A room's outbox outlives its panel (that is how a queue survives leaving
  // the workspace); each test starts with none.
  forgetRoomOutboxes();
  visual.notifyConversationUpdate.mockClear();
  visual.setRender.mockClear();
  history = [...HISTORY];
  sendReplies = [];
  sends.length = 0;
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/chat/history")) return reply({ messages: history, hasMore: url.includes("before=") ? false : true });
    if (url === "/api/chat/send") {
      const body = JSON.parse(String(init?.body ?? "{}"));
      sends.push(body);
      // Joining a turn that is not running: the engine hands it back.
      if (body.join_running) return sseResponse([{ event: "followup_queued", data: {} }]);
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

function attach(...files: File[]) {
  fireEvent.change(screen.getByTestId("attach-input"), { target: { files } });
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
    // The room by id, and the outbox entry it was sent from (its client id).
    expect(sends[0]).toEqual({ message: "And margins?", request_id: expect.any(String), room: REPORT_ID, client_id: expect.any(String) });
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
    await waitFor(() => expect(screen.queryByTestId("room-busy-notice")).toBeNull());

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

  it("draws a turn's charts in the room and never touches the main conversation's canvas", async () => {
    history = [];
    const chart = { component: "render_bar_chart", props: { title: "Margins by week", data: [{ name: "W1", value: 3 }], dataKey: "value" } };
    const long = "Here is the breakdown. ".repeat(12);
    sendReplies.push(() =>
      sseResponse([
        { event: "room", data: { roomId: REPORT_ID } },
        { event: "delta", data: { text: `${long}[RENDER:${chart.component}:${JSON.stringify(chart.props)}]` } },
        { event: "render", data: { component: "render_markdown", props: { content: "Footnote." } } },
        { event: "dashboard", data: { data: { revenue: 10 } } },
        { event: "done", data: { text: long } },
      ]),
    );
    render(<ChatPanel variant="page" room={{ id: REPORT_ID }} />);
    await screen.findByTestId("room-chat-empty");
    await type("Show margins");
    expect(await screen.findByTestId("bar-chart")).toHaveTextContent("Margins by week");
    expect(await screen.findByTestId("markdown-view")).toHaveTextContent("Footnote.");
    expect(screen.getByTestId("message-renders")).toBeInTheDocument();
    expect(visual.setRender).not.toHaveBeenCalled();
    expect(visual.notifyConversationUpdate).not.toHaveBeenCalled();
  });

  it("sends a queued message's files with it when the room frees up", async () => {
    history = [];
    sendReplies.push(() => reply({ error: "room_busy", answering: "Alice", request_admitted: false }, 409));
    const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByTestId("room-chat-empty");
    attach(new File(["a,b"], "q4.csv", { type: "text/csv" }));
    await screen.findByTestId("attachment-chip");
    await type("See attached");
    await screen.findByTestId("room-busy-notice");
    expect(sends[0].attachments).toHaveLength(1);
    await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
    await screen.findByText("Noted.");
    expect(sends).toHaveLength(2);
    expect(sends[1]).toMatchObject({ message: "See attached", attachments: [expect.objectContaining({ name: "q4.csv" })] });
    expect(screen.getAllByTestId("message-attachment")).toHaveLength(1);
  });

  it("queues and later sends a message that is only a file", async () => {
    history = [];
    sendReplies.push(() => reply({ error: "room_busy", answering: "Alice", request_admitted: false }, 409));
    const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByTestId("room-chat-empty");
    attach(new File(["x"], "only.txt", { type: "text/plain" }));
    await screen.findByTestId("attachment-chip");
    await act(async () => {
      fireEvent.click(screen.getByTestId("send-button"));
    });
    await screen.findByTestId("room-busy-notice");
    await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
    await screen.findByText("Noted.");
    expect(sends[1]).toMatchObject({ message: "", attachments: [expect.objectContaining({ name: "only.txt" })] });
    await waitFor(() => expect(screen.queryByTestId("room-busy-notice")).toBeNull());
    expect(screen.queryByText(/Queued/)).toBeNull();
  });

  it("a file typed while others wait joins the queue with its file, not ahead of them", async () => {
    history = [];
    sendReplies.push(() => reply({ error: "room_busy", answering: "Alice", request_admitted: false }, 409));
    const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByTestId("room-chat-empty");
    await type("first");
    await screen.findByTestId("room-busy-notice");
    attach(new File(["x"], "later.txt", { type: "text/plain" }));
    await screen.findByTestId("attachment-chip");
    await act(async () => {
      fireEvent.click(screen.getByTestId("send-button"));
    });
    expect(sends).toHaveLength(1);
    await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
    await waitFor(() => expect(sends).toHaveLength(3));
    expect(sends[1].message).toBe("first");
    expect(sends[2]).toMatchObject({ attachments: [expect.objectContaining({ name: "later.txt" })] });
  });

  it("says an archived workspace can be read but not posted in", async () => {
    render(<ChatPanel variant="page" room={{ id: REPORT_ID, readOnly: true, readOnlyReason: "archived" }} />);
    await screen.findByText("How did we do?");
    expect(screen.getByTestId("chat-input")).toBeDisabled();
    expect(screen.getByTestId("room-readonly-notice")).toHaveTextContent(/archived/i);
  });

  it("a message typed while the FIRST send is in flight stays behind it when that send is refused", async () => {
    history = [];
    let answer!: (r: Response) => void;
    sendReplies.push(() => new Promise<Response>((r) => (answer = r)));
    const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByTestId("room-chat-empty");
    await type("A");
    await waitFor(() => expect(sends).toHaveLength(1));
    await type("B");
    await act(async () => answer(reply({ error: "room_busy", answering: "Alice", request_admitted: false }, 409)));
    await screen.findByTestId("room-busy-notice");
    await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
    await waitFor(() => expect(sends.map((s) => s.message)).toEqual(["A", "A", "B"]));
    // Nothing else sends a room message: no steering, no joining a turn.
    expect(sends.every((s) => !s.join_running)).toBe(true);
    expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/chat/steer")).toBe(false);
    expect(screen.getAllByText("A")).toHaveLength(1);
    expect(screen.getAllByText("B")).toHaveLength(1);
  });

  for (const echo of [true, false]) {
    it(`shows a message once after leaving while it was answered and coming back (client id ${echo ? "echoed" : "not echoed"})`, async () => {
      history = [];
      let answer!: (r: Response) => void;
      sendReplies.push(() => new Promise<Response>((r) => (answer = r)));
      const first = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
      await screen.findByTestId("room-chat-empty");
      await type("A");
      await waitFor(() => expect(sends).toHaveLength(1));
      first.unmount();

      // The engine has stored it; the reply is still on its way.
      history = [{ id: 40, role: "user", content: "A", authorName: "Alice", ...(echo ? { clientId: sends[0].client_id } : {}) }];
      const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
      await screen.findByText("A");
      expect(screen.getAllByText("A")).toHaveLength(1);

      await act(async () => answer(sseResponse([{ event: "done", data: { text: "Noted." } }])));
      history = [...history, { id: 41, role: "assistant", content: "Noted.", authorName: "Analyst" }];
      await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
      await screen.findByText("Noted.");
      expect(screen.getAllByText("A")).toHaveLength(1);
      expect(sends).toHaveLength(1);
      // Every room send names its outbox entry.
      expect(sends[0].client_id).toEqual(expect.stringMatching(/^[0-9a-f-]{36}$/));
    });
  }

  it("shows a turn's answer once when its sender left mid-answer and the turn finished without them", async () => {
    // The turn outlives its sender's stream: by the time they are back its
    // answer is stored. The request the old page journaled is not drawn again
    // as a recovered answer beside the stored one.
    const scope = "3b3b3b3b-0000-4000-8000-000000000003";
    const request = "3c3c3c3c-0000-4000-8000-000000000004";
    sessionStorage.setItem(`helm.chat.pending.v1.${scope}`, JSON.stringify([request]));
    history = [
      { id: 60, role: "user", content: "Same words", authorName: "Bob" },
      { id: 61, role: "assistant", content: "Answering Bob: Same words", authorName: "Analyst" },
    ];
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/chat/history")) return reply({ messages: history, hasMore: false, recoveryScope: scope });
      if (url.startsWith("/api/chat/outcome")) return reply({ status: "completed", text: "Answering Bob: Same words" });
      return reply({ messages: [], agents: [] });
    });

    render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByText("Answering Bob: Same words");
    await act(async () => new Promise((r) => setTimeout(r, 50)));

    expect(screen.getAllByText("Answering Bob: Same words")).toHaveLength(1);
    expect(urls().some((u) => u.startsWith("/api/chat/outcome"))).toBe(false);
    sessionStorage.clear();
  });

  const lostAnswers = {
    "a 504": () => reply({ error: "gateway timeout" }, 504),
    "a stream cut mid-answer": () =>
      new Response(
        new ReadableStream({
          start(c) {
            c.enqueue(new TextEncoder().encode(`event: delta\ndata: {"text":"Work"}\n\n`));
            c.error(new Error("connection reset"));
          },
        }),
        { status: 200, headers: { "content-type": "text/event-stream" } },
      ),
  };
  for (const [how, lost] of Object.entries(lostAnswers)) {
    for (const echo of [true, false]) {
      it(`shows a stored message once, never as not sent, when its answer was lost to ${how} while away (client id ${echo ? "echoed" : "not echoed"})`, async () => {
        history = [];
        let answer!: (r: Response) => void;
        sendReplies.push(() => new Promise<Response>((r) => (answer = r)));
        const first = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
        await screen.findByTestId("room-chat-empty");
        await type("Kept");
        await waitFor(() => expect(sends).toHaveLength(1));
        first.unmount();
        // The engine admitted and stored it; only the answer was lost.
        await act(async () => answer(lost()));
        history = [{ id: 50, role: "user", content: "Kept", authorName: "Alice", ...(echo ? { clientId: sends[0].client_id } : {}) }];
        render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
        expect(await screen.findByText("Kept")).toBeInTheDocument();
        await act(async () => {});
        expect(screen.getAllByText("Kept")).toHaveLength(1);
        expect(screen.queryByText(/not sent/i)).toBeNull();
        expect(sends).toHaveLength(1);
      });
    }
  }

  it("still shows a refused message as not sent when an earlier stored message has the same text", async () => {
    history = [{ id: 30, role: "user", content: "thanks", authorName: "Bob" }];
    let answer!: (r: Response) => void;
    sendReplies.push(() => new Promise<Response>((r) => (answer = r)));
    const first = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByText("thanks");
    await type("thanks");
    await waitFor(() => expect(sends).toHaveLength(1));
    first.unmount();
    await act(async () => answer(reply({ error: "refused", request_admitted: false }, 502)));
    render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await waitFor(() => expect(screen.getAllByText("thanks")).toHaveLength(2));
    expect(screen.getByTestId("message-note")).toHaveTextContent(/not sent/i);
  });

  describe("a refused message stays marked as not sent when the same text is stored later (no client id echoed)", () => {
    async function refuseWhileAway() {
      history = [];
      let answer!: (r: Response) => void;
      sendReplies.push(() => new Promise<Response>((r) => (answer = r)));
      const first = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
      await screen.findByTestId("room-chat-empty");
      await type("thanks");
      await waitFor(() => expect(sends).toHaveLength(1));
      first.unmount();
      await act(async () => answer(reply({ error: "refused", request_admitted: false }, 502)));
      const view = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
      await waitFor(() => expect(screen.getByTestId("message-note")).toHaveTextContent(/not sent/i));
      return view;
    }

    it("the reader retypes it: the retry shows once, the refusal stays marked", async () => {
      const { rerender } = await refuseWhileAway();
      await type("thanks");
      await waitFor(() => expect(sends).toHaveLength(2));
      await screen.findByText("Noted.");
      history = [{ id: 60, role: "user", content: "thanks", authorName: "Alice" }];
      await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
      await waitFor(() => expect(screen.getAllByTestId("message-author").map((a) => a.textContent)).toContain("Alice"));
      expect(screen.getAllByText("thanks")).toHaveLength(2);
      expect(screen.getByTestId("message-note")).toHaveTextContent(/not sent/i);
    });

    it("refused in this panel: another member's later same-text message is added beside it, never in its place", async () => {
      history = [];
      sendReplies.push(() => reply({ error: "refused", request_admitted: false }, 502));
      const { rerender } = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
      await screen.findByTestId("room-chat-empty");
      await type("thanks");
      await screen.findByText("refused");
      history = [{ id: 63, role: "user", content: "thanks", authorName: "Bob" }];
      await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
      await waitFor(() => expect(screen.getAllByTestId("message-author").map((a) => a.textContent)).toContain("Bob"));
      const thanksAuthors = screen
        .getAllByTestId("message-row")
        .filter((row) => row.textContent?.includes("thanks"))
        .map((row) => row.querySelector('[data-testid="message-author"]')?.textContent);
      expect(thanksAuthors).toEqual(["You", "Bob"]);
    });

    it("another member's later same-text message does not clear it", async () => {
      const { rerender } = await refuseWhileAway();
      history = [{ id: 61, role: "user", content: "thanks", authorName: "Bob" }];
      await act(async () => rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
      await waitFor(() => expect(screen.getAllByText("thanks")).toHaveLength(2));
      expect(screen.getByTestId("message-note")).toHaveTextContent(/not sent/i);
    });
  });

  it("says a message may not have arrived, not that it was not sent, when its outcome is unknown", async () => {
    history = [];
    let answer!: (r: Response) => void;
    sendReplies.push(() => new Promise<Response>((r) => (answer = r)));
    const first = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    await screen.findByTestId("room-chat-empty");
    await type("Unsure");
    await waitFor(() => expect(sends).toHaveLength(1));
    first.unmount();
    await act(async () => answer(reply({ error: "gateway timeout" }, 504)));
    render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
    expect(await screen.findByText("Unsure")).toBeInTheDocument();
    expect(screen.getByTestId("message-note")).toHaveTextContent(/may not have reached/i);
    expect(screen.queryByText(/not sent/i)).toBeNull();
  });

  for (const when of ["before", "after"] as const) {
    it(`shows a message as not sent when its send fails while the reader is away (failure ${when} they return)`, async () => {
      history = [];
      let answer!: (r: Response) => void;
      sendReplies.push(() => new Promise<Response>((r) => (answer = r)));
      const first = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
      await screen.findByTestId("room-chat-empty");
      await type("Keep this");
      await waitFor(() => expect(sends).toHaveLength(1));
      first.unmount();

      const failure = () => answer(reply({ error: "the engine did not confirm this workspace", request_admitted: false }, 502));
      if (when === "before") await act(async () => failure());
      render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
      if (when === "after") {
        await screen.findByTestId("room-chat-empty");
        await act(async () => failure());
      }
      expect(await screen.findByText("Keep this")).toBeInTheDocument();
      expect(screen.getByTestId("message-note")).toHaveTextContent(/not sent/i);
      expect(screen.getAllByText("Keep this")).toHaveLength(1);
      // Shown once, by this panel: nothing is sent again behind the reader's back.
      expect(sends).toHaveLength(1);
    });
  }

  describe("a message typed while a queued one is being offered again", () => {
    /** A queued "A", offered again with its answer held; "B" typed meanwhile. */
    async function typeDuringRetry(retryAnswer: Promise<Response>) {
      history = [];
      sendReplies.push(() => reply({ error: "room_busy", answering: "Alice", request_admitted: false }, 409));
      sendReplies.push(() => retryAnswer);
      const view = render(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 0 }} />);
      await screen.findByTestId("room-chat-empty");
      await type("A");
      await screen.findByTestId("room-busy-notice");
      await act(async () => view.rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 1 }} />));
      await waitFor(() => expect(sends.filter((s) => !s.join_running)).toHaveLength(2));
      await type("B");
      return view;
    }
    const turns = () => sends.filter((s) => !s.join_running).map((s) => s.message);

    it("room still busy: B waits behind A, and A goes first", async () => {
      let answer!: (r: Response) => void;
      const view = await typeDuringRetry(new Promise<Response>((r) => (answer = r)));
      await act(async () => answer(reply({ error: "room_busy", answering: "Alice", request_admitted: false }, 409)));
      await waitFor(() => expect(screen.getAllByTestId("message-note")).toHaveLength(2));
      expect(turns()).toEqual(["A", "A"]);
      // The room frees up: A, then B, one turn each.
      await act(async () => view.rerender(<ChatPanel variant="page" room={{ id: REPORT_ID, refreshKey: 2 }} />));
      await waitFor(() => expect(turns()).toEqual(["A", "A", "A", "B"]));
      expect(screen.getAllByText("B")).toHaveLength(1);
    });

    it("room free: A is admitted, then B goes after it", async () => {
      let answer!: (r: Response) => void;
      await typeDuringRetry(new Promise<Response>((r) => (answer = r)));
      await act(async () => answer(sseResponse([{ event: "done", data: { text: "Noted." } }])));
      await waitFor(() => expect(turns()).toEqual(["A", "A", "B"]));
      expect(screen.getAllByText("B")).toHaveLength(1);
    });
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
  it("still draws charts on the main canvas and hands long replies to it", async () => {
    history = [];
    const chart = { component: "render_bar_chart", props: { title: "Main chart", data: [], dataKey: "value" } };
    const long = "A long answer. ".repeat(20);
    sendReplies.push(() =>
      sseResponse([
        { event: "delta", data: { text: `${long}[RENDER:${chart.component}:${JSON.stringify(chart.props)}]` } },
        { event: "done", data: { text: long } },
      ]),
    );
    render(<ChatPanel variant="page" />);
    await type("main");
    await screen.findByText(long.trim());
    expect(visual.setRender).toHaveBeenCalledWith({ component: "render_bar_chart", props: chart.props });
    expect(visual.notifyConversationUpdate).toHaveBeenCalled();
    expect(screen.queryByTestId("message-renders")).toBeNull();
  });

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
