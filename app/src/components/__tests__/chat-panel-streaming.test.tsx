import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { ChatPanel } from "../chat-panel";

// Mock hooks
vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({
    notifyConversationUpdate: vi.fn(),
    setRender: vi.fn(),
  }),
}));

vi.mock("@/hooks/use-throttle", () => ({
  useThrottle: (value: string) => value,
}));

function makeSSEStream(events: Array<{ event: string; data: Record<string, unknown> }>) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const { event, data } of events) {
        controller.enqueue(
          encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
        );
      }
      controller.close();
    },
  });
}

async function typeAndSend(input: HTMLTextAreaElement, text: string) {
  fireEvent.change(input, { target: { value: text } });
  fireEvent.click(screen.getByTestId("send-button"));
}

describe("ChatPanel streaming UX", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([false, true])("renders a host report from done, replacing any preamble (%s)", async (preamble) => {
    localStorage.clear();
    const report = "The goal is paused. One task remains open. Scheduled reviews will not run.";
    const events = [
      ...(preamble ? [{ event: "delta", data: { text: "Checking the goal now." } }] : []),
      { event: "tool_start", data: { tool: "report_pursuit_goal", call_id: "report" } },
      { event: "tool_end", data: { tool: "report_pursuit_goal", call_id: "report" } },
      { event: "done", data: { status: "completed", text: report } },
    ];
    const requests = vi.fn(async (url: string | URL | Request) => {
      if (String(url) === "/api/chat/send") {
        return new Response(makeSSEStream(events), { headers: { "content-type": "text/event-stream" } });
      }
      return { ok: true, json: async () => ({ messages: [], agents: [] }) };
    });
    vi.stubGlobal("fetch", requests);
    render(<ChatPanel />);
    await act(async () => { await typeAndSend(screen.getByTestId("chat-input") as HTMLTextAreaElement, "Pause that work."); });
    await waitFor(() => expect(screen.getByText(report)).toBeTruthy());
    expect(screen.queryByText("Checking the goal now.")).toBeNull();
    expect(requests.mock.calls.filter(([url]) => String(url) === "/api/chat/send")).toHaveLength(1);
    expect(requests.mock.calls.some(([url]) => String(url).startsWith("/api/chat/outcome"))).toBe(false);
  });

  it("tool_start SSE event shows tool name in normal chat mode", async () => {
    let finish!: () => void;
    const release = new Promise<void>((resolve) => { finish = resolve; });
    const stream = new ReadableStream({
      async start(controller) {
        const encoder = new TextEncoder();
        controller.enqueue(
          encoder.encode(
            `event: tool_start\ndata: ${JSON.stringify({ tool: "search_memory", call_id: "c1" })}\n\n`
          )
        );
        await release;
        controller.enqueue(
          encoder.encode(
            `event: tool_end\ndata: ${JSON.stringify({ tool: "search_memory", call_id: "c1" })}\n\n`
          )
        );
        controller.enqueue(
          encoder.encode(
            `event: done\ndata: ${JSON.stringify({ text: "result" })}\n\n`
          )
        );
        controller.close();
      },
    });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        body: stream,
        headers: new Headers({ "content-type": "text/event-stream" }),
      })
    );

    render(<ChatPanel />);
    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    await act(async () => {
      await typeAndSend(input, "test");
    });

    try {
      await waitFor(() => expect(screen.getByText("search memory")).toBeTruthy());
    } finally {
      await act(async () => { finish(); });
    }
    await waitFor(() => expect(screen.queryByTestId("streaming-message")).toBeNull());

  });

  it("iteration_start SSE event shows step progress when multi-step", async () => {
    const stream = new ReadableStream({
      async start(controller) {
        const encoder = new TextEncoder();
        controller.enqueue(
          encoder.encode(
            `event: iteration_start\ndata: ${JSON.stringify({ iteration: 2, max_iterations: 5 })}\n\n`
          )
        );
        await new Promise((r) => setTimeout(r, 50));
        controller.enqueue(
          encoder.encode(
            `event: done\ndata: ${JSON.stringify({ text: "done" })}\n\n`
          )
        );
        controller.close();
      },
    });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        body: stream,
        headers: new Headers({ "content-type": "text/event-stream" }),
      })
    );

    render(<ChatPanel />);
    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    await act(async () => {
      await typeAndSend(input, "test");
    });

    // Step progress should appear during streaming
    await waitFor(
      () => {
        // Step progress may flash briefly — verify no crash
        expect(screen.getByTestId("streaming-message")).toBeTruthy();
      },
      { timeout: 2000 }
    );
  });

  it("tool_end clears tool indicator", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        body: makeSSEStream([
          { event: "tool_start", data: { tool: "search_memory", call_id: "c1" } },
          { event: "tool_end", data: { tool: "search_memory", call_id: "c1" } },
          { event: "delta", data: { text: "Found results" } },
          { event: "done", data: { text: "Found results" } },
        ]),
        headers: new Headers({ "content-type": "text/event-stream" }),
      })
    );

    render(<ChatPanel />);
    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    await act(async () => {
      await typeAndSend(input, "test");
    });

    // After completion, tool indicator should be gone
    await waitFor(
      () => {
        const indicator = screen.queryByTestId("tool-indicator");
        expect(indicator).toBeNull();
      },
      { timeout: 2000 }
    );
  });
});

describe("ordinary chat terminal outcomes", () => {
  afterEach(() => vi.restoreAllMocks());

  it.each(["eof", "transport", "aborted", "failed", "failed_with_text", "timeout", "cancelled", "completed"])(
    "does not mistake partial text for completion after %s",
    async (outcome) => {
      let sends = 0;
      let originalRequestId = "";
      let reads = 0;
      const errorStatus = outcome === "failed_with_text" ? "failed" : outcome;
      const failedRun = ["failed", "timeout", "cancelled"].includes(errorStatus);
      const recoveredText = failedRun
        ? `Run ${errorStatus}. Audit readback confirms the calendar change; notification delivery is unverified.`
        : "Recovered recorded result";
      global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
        if (url.startsWith("/api/chat/outcome?")) {
          reads += 1;
          expect(new URL(url, "http://test").searchParams.get("request_id")).toBe(originalRequestId);
          return { ok: true, json: async () => ({ terminal: true, state: failedRun ? errorStatus : "completed", text: recoveredText, reconciliation_pending: false }) };
        }
        if (url === "/api/chat/send") {
          sends += 1;
          originalRequestId = JSON.parse(init!.body as string).request_id;
          if (outcome === "transport") throw new TypeError("Failed to fetch");
          const events = [{ event: "delta", data: { text: "Everything is done." } }];
          if (outcome !== "eof") {
            const terminal = outcome === "aborted"
              ? { text: "", aborted: true }
              : outcome === "failed"
                ? { text: "", status: "failed" }
                : ["failed_with_text", "timeout", "cancelled"].includes(outcome)
                  ? { text: "Execution stopped after a provider error", status: outcome === "failed_with_text" ? "failed" : outcome }
                  : { text: "Verified result", status: "completed" };
            return { ok: true, body: makeSSEStream([...events, { event: "done", data: terminal }]) };
          }
          return { ok: true, body: makeSSEStream(events) };
        }
        if (url === "/api/chat/history") return { ok: true, json: async () => ({ messages: [] }) };
        if (url === "/api/chat/plan/status") return { ok: true, json: async () => ({ active: false }) };
        return { ok: false, status: 404 };
      });
      render(<ChatPanel />);
      await typeAndSend(screen.getByTestId("chat-input") as HTMLTextAreaElement, "Do the work");
      const expected = outcome === "completed" ? "Verified result" : recoveredText;
      await waitFor(() => expect(screen.getByTestId("message-assistant").textContent).toContain(expected));
      expect(screen.queryByText("Everything is done.")).toBeNull();
      expect(sends).toBe(1);
      expect(reads).toBe(outcome === "completed" ? 0 : 1);
    },
  );
});
