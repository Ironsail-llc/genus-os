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

type Call = { url: string; method: string; body: Record<string, unknown> };

/** The engine as the browser sees it, with steer mode on or off. */
function engine(running: ReadableStream, steerEnabled: boolean) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ url: u, method, body });
    if (u === "/api/chat/steer" && method === "GET") {
      return new Response(JSON.stringify({ enabled: steerEnabled }), { headers: { "content-type": "application/json" } });
    }
    if (u === "/api/chat/steer" || u === "/api/chat/interrupt") {
      return new Response(JSON.stringify({ ok: true, found: true }), { headers: { "content-type": "application/json" } });
    }
    if (u === "/api/chat/abort") {
      return new Response(JSON.stringify({ ok: true, aborted: true, durable_stopped: true }), { headers: { "content-type": "application/json" } });
    }
    if (u === "/api/chat/send") {
      if (body.join_running) {
        return new Response(sse("followup_joined", {}), { headers: { "content-type": "text/event-stream" } });
      }
      return new Response(running, { headers: { "content-type": "text/event-stream" } });
    }
    return { ok: true, json: async () => ({ messages: [], agents: [] }) };
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

async function type(text: string) {
  const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
  await act(async () => {
    fireEvent.change(input, { target: { value: text } });
    fireEvent.keyDown(input, { key: "Enter" });
  });
}

const posts = (calls: Call[], url: string) => calls.filter((c) => c.url === url && c.method === "POST");

describe("steering a running task from the Helm", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("turns the composer into a steer input while a reply streams, and sends to the run", async () => {
    const running = heldStream([{ event: "delta", data: { text: "Working" } }], [{ event: "done", data: { text: "Done." } }]);
    const calls = engine(running.body, true);
    render(<ChatPanel />);
    await type("Draft the report.");
    await waitFor(() => expect(screen.getByTestId("abort-button")).toBeTruthy());

    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    await waitFor(() => expect(input.placeholder).toBe("Steer the running task..."));

    await type("Use the EU region.");
    await waitFor(() => expect(screen.getByText("Steering the run")).toBeTruthy());
    expect(posts(calls, "/api/chat/steer")).toHaveLength(1);
    const turn = posts(calls, "/api/chat/send")[0].body.request_id;
    expect(turn).toBeTruthy();
    // It names the turn it is for: two tabs on one agent are two runs.
    expect(posts(calls, "/api/chat/steer")[0].body).toMatchObject({ text: "Use the EU region.", request_id: turn });
    expect(posts(calls, "/api/chat/send").filter((c) => c.body.join_running)).toHaveLength(0);

    await act(async () => { running.release(); });
    await waitFor(() => expect(screen.getByText("Done.")).toBeTruthy());
    expect(input.placeholder).not.toBe("Steer the running task...");
  });

  it("stops the run at its next step with Stop", async () => {
    const running = heldStream([{ event: "delta", data: { text: "Working" } }], [{ event: "done", data: { text: "", status: "cancelled" } }]);
    const calls = engine(running.body, true);
    render(<ChatPanel />);
    await type("Draft the report.");
    await waitFor(() => expect(screen.getByTestId("chat-input").getAttribute("placeholder")).toBe("Steer the running task..."));

    await act(async () => { fireEvent.click(screen.getByTestId("abort-button")); });
    await waitFor(() => expect(posts(calls, "/api/chat/interrupt")).toHaveLength(1));
    expect(posts(calls, "/api/chat/interrupt")[0].body.request_id).toBe(posts(calls, "/api/chat/send")[0].body.request_id);
    expect(posts(calls, "/api/chat/abort")).toHaveLength(0);
    await act(async () => { running.release(); });
  });

  it("with steer mode off, sends follow-ups and stops exactly as before", async () => {
    const running = heldStream([{ event: "delta", data: { text: "Working" } }], [{ event: "done", data: { text: "Done." } }]);
    const calls = engine(running.body, false);
    render(<ChatPanel />);
    await type("Draft the report.");
    await waitFor(() => expect(screen.getByTestId("abort-button")).toBeTruthy());
    expect((screen.getByTestId("chat-input") as HTMLTextAreaElement).placeholder).toBe("Add to the running task...");

    await type("Use the EU region.");
    await waitFor(() => expect(screen.getByText("Added to the running task")).toBeTruthy());
    expect(posts(calls, "/api/chat/steer")).toHaveLength(0);

    await act(async () => { fireEvent.click(screen.getByTestId("abort-button")); });
    await waitFor(() => expect(posts(calls, "/api/chat/abort")).toHaveLength(1));
    expect(posts(calls, "/api/chat/interrupt")).toHaveLength(0);
    await act(async () => { running.release(); });
  });
});
