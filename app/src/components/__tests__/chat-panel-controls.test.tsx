import { afterEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ChatPanel } from "../chat-panel";

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ notifyConversationUpdate: vi.fn(), setRender: vi.fn() }),
}));
vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (value: string) => value }));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function startRequest() {
  let stream!: ReadableStreamDefaultController<Uint8Array>;
  let signal!: AbortSignal;
  let acknowledge!: (value: unknown) => void;
  const body = new ReadableStream<Uint8Array>({ start(controller) { stream = controller; } });
  const stop = new Promise(resolve => { acknowledge = resolve; });
  const fetch = vi.fn((url: string, init?: RequestInit) => {
    if (url === "/api/chat/send") {
      signal = init!.signal!;
      signal.addEventListener("abort", () => stream.error(new DOMException("Aborted", "AbortError")));
      return Promise.resolve({ ok: true, body, headers: new Headers({ "content-type": "text/event-stream" }) });
    }
    if (url === "/api/chat/abort") return stop;
    return Promise.resolve({ ok: true, json: async () => ({ messages: [], agents: [], active: false }) });
  });
  vi.stubGlobal("fetch", fetch);
  render(<ChatPanel />);
  fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "Perform the synthetic action" } });
  fireEvent.click(screen.getByTestId("send-button"));
  await waitFor(() => expect(signal).toBeDefined());
  const event = async (type: string, data: object) => {
    await act(async () => stream.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`)));
  };
  return { signal, stream, acknowledge, fetch, event };
}

it("renders acceptance and elapsed progress, then waits for durable stop acknowledgement", async () => {
  const request = await startRequest();
  await request.event("accepted", { text: "Request accepted" });
  expect(await screen.findByText("Request accepted")).toBeVisible();
  await request.event("progress", { text: "Checking the fixture receipt", elapsed_s: 10 });
  expect(await screen.findByText("Checking the fixture receipt · 10s elapsed")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Stop request" }));
  expect(await screen.findByText("Stopping…")).toBeVisible();
  expect(request.signal.aborted).toBe(false);
  const sent = JSON.parse(request.fetch.mock.calls.find(([url]) => url === "/api/chat/send")![1]!.body as string);
  const stopped = JSON.parse(request.fetch.mock.calls.find(([url]) => url === "/api/chat/abort")![1]!.body as string);
  expect(stopped.request_id).toBe(sent.request_id);
  expect(sent.request_id).toMatch(/^[0-9a-f-]{36}$/);
  await act(async () => request.acknowledge({ ok: true, json: async () => ({ ok: true, aborted: true, durable_stopped: true }) }));
  await waitFor(() => expect(request.signal.aborted).toBe(true));
  expect(await screen.findByText(/Stop acknowledged. Already dispatched requests may finish/)).toBeVisible();
});

it("does not claim a stop or disconnect when durable acknowledgement fails", async () => {
  const request = await startRequest();
  fireEvent.click(screen.getByRole("button", { name: "Stop request" }));
  await act(async () => request.acknowledge({ ok: false, status: 502 }));
  expect(await screen.findByText(/Stop could not be confirmed/)).toBeVisible();
  expect(request.signal.aborted).toBe(false);
  expect(screen.getByRole("button", { name: "Stop request" })).toBeEnabled();
  await act(async () => request.stream.close());
});


it("a delayed stop receipt cannot disconnect the next request", async () => {
  const first = await startRequest();
  fireEvent.click(screen.getByRole("button", { name: "Stop request" }));
  await act(async () => first.stream.close());
  await waitFor(() => expect(screen.getByTestId("send-button")).toBeVisible());
  let secondStream!: ReadableStreamDefaultController<Uint8Array>;
  let secondSignal!: AbortSignal;
  first.fetch.mockImplementation((url: string, init?: RequestInit) => {
    if (url === "/api/chat/send") {
      secondSignal = init!.signal!;
      return Promise.resolve({ ok: true,
        body: new ReadableStream<Uint8Array>({ start(controller) { secondStream = controller; } }),
        headers: new Headers({ "content-type": "text/event-stream" }),
      });
    }
    return Promise.resolve({ ok: true, json: async () => ({}) });
  });
  fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "A new request" } });
  fireEvent.click(screen.getByTestId("send-button"));
  await waitFor(() => expect(secondSignal).toBeDefined());
  await act(async () => first.acknowledge({ ok: true, json: async () => ({ ok: true, durable_stopped: true }) }));
  expect(secondSignal.aborted).toBe(false);
  expect(screen.queryByText(/Stop acknowledged/)).toBeNull();
  expect(screen.getByRole("button", { name: "Stop request" })).toBeEnabled();
  await act(async () => secondStream.close());
});
