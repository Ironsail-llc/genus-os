import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { ChatPanel } from "../chat-panel";

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ notifyConversationUpdate: vi.fn(), setRender: vi.fn() }),
}));
vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (value: string) => value }));

type Event = { event: string; data: Record<string, unknown> };

function sse(event: string, data: Record<string, unknown>) {
  return new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

/** A running turn's stream: `head` now, then `tail` once `release()` is called. */
function heldStream(head: Event[], tail: Event[]) {
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  const body = new ReadableStream({
    async start(controller) {
      for (const e of head) controller.enqueue(sse(e.event, e.data));
      await held;
      for (const e of tail) controller.enqueue(sse(e.event, e.data));
      controller.close();
    },
  });
  return { body, release };
}

function stream(events: Event[]) {
  return new ReadableStream({
    start(controller) {
      for (const e of events) controller.enqueue(sse(e.event, e.data));
      controller.close();
    },
  });
}

function sseResponse(body: ReadableStream) {
  return new Response(body, { headers: { "content-type": "text/event-stream" } });
}

/** Routes /api/chat/send: the first unflagged send gets `running`; flagged ones get `reply`. */
function engine(running: ReadableStream, reply: string, later: Event[] = [{ event: "done", data: { text: "second answer" } }]) {
  const sends: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    if (String(url) === "/api/chat/send") {
      const body = JSON.parse(String(init?.body ?? "{}"));
      sends.push(body);
      if (body.join_running) return sseResponse(stream([{ event: reply, data: { event: reply } }]));
      return sseResponse(sends.filter((s) => !s.join_running).length === 1 ? running : stream(later));
    }
    return { ok: true, json: async () => ({ messages: [], agents: [] }) };
  });
  vi.stubGlobal("fetch", fetchMock);
  return sends;
}

async function type(text: string) {
  const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
  await act(async () => {
    fireEvent.change(input, { target: { value: text } });
    fireEvent.keyDown(input, { key: "Enter" });
  });
}

describe("adding to a running task in the Helm", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("keeps the composer open while the agent works, and a message sent then joins the task", async () => {
    const running = heldStream([{ event: "delta", data: { text: "Working" } }], [{ event: "done", data: { text: "Done, with Y." } }]);
    const sends = engine(running.body, "followup_joined");
    render(<ChatPanel />);
    await type("Draft it.");
    await waitFor(() => expect(screen.getByTestId("abort-button")).toBeTruthy());

    expect((screen.getByTestId("chat-input") as HTMLTextAreaElement).disabled).toBe(false);
    await type("Also include Y.");
    await waitFor(() => expect(screen.getByText("Added to the running task")).toBeTruthy());
    expect(sends[1]).toMatchObject({ message: "Also include Y.", join_running: true });

    await act(async () => { running.release(); });
    await waitFor(() => expect(screen.getByText("Done, with Y.")).toBeTruthy());
    expect(sends.filter((s) => !s.join_running)).toHaveLength(1);
  });

  it("shows a superseded answer as its own message, then the revised one", async () => {
    const running = heldStream(
      [
        { event: "delta", data: { text: "Forecast: rain." } },
        { event: "interim", data: { text: "Forecast: rain." } },
        { event: "delta", data: { text: "Tides: high at 8." } },
      ],
      [{ event: "done", data: { text: "Tides: high at 8." } }],
    );
    engine(running.body, "followup_joined");
    render(<ChatPanel />);
    await type("Weather?");
    await act(async () => { running.release(); });

    await waitFor(() => expect(screen.getByText("Tides: high at 8.")).toBeTruthy());
    const answers = screen.getAllByTestId("message-assistant").map((el) => el.textContent);
    expect(answers).toEqual(["Forecast: rain.", "Tides: high at 8."]);
  });

  it("sends what the task never took as the next turn, once, after the stream ends", async () => {
    const running = heldStream([], [
      { event: "done", data: { text: "First answer." } },
      { event: "pending_followups", data: { messages: ["Also include Y."] } },
    ]);
    const sends = engine(running.body, "followup_joined");
    render(<ChatPanel />);
    await type("Draft it.");
    await waitFor(() => expect(screen.getByTestId("abort-button")).toBeTruthy());
    await type("Also include Y.");
    await waitFor(() => expect(screen.getByText("Added to the running task")).toBeTruthy());

    await act(async () => { running.release(); });
    await waitFor(() => expect(screen.getByText("second answer")).toBeTruthy());
    const ordinary = sends.filter((s) => !s.join_running);
    expect(ordinary.map((s) => s.message)).toEqual(["Draft it.", "Also include Y."]);
    expect(screen.getAllByText("Also include Y.")).toHaveLength(1);
  });

  it("holds a queued message and sends it when the running task ends", async () => {
    const running = heldStream([], [{ event: "done", data: { text: "First answer." } }]);
    const sends = engine(running.body, "followup_queued");
    render(<ChatPanel />);
    await type("Draft it.");
    await waitFor(() => expect(screen.getByTestId("abort-button")).toBeTruthy());
    await type("Then do Z.");
    await waitFor(() => expect(screen.getByText("Queued — sends when this reply finishes")).toBeTruthy());

    await act(async () => { running.release(); });
    await waitFor(() => expect(screen.getByText("second answer")).toBeTruthy());
    expect(sends.filter((s) => !s.join_running).map((s) => s.message)).toEqual(["Draft it.", "Then do Z."]);
  });
});
