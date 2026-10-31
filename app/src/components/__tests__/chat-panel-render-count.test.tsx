/**
 * What a streaming delta costs.
 *
 * Every delta sets the panel's streaming text, which re-renders ChatPanel. If
 * the history bubbles re-render with it, every one of them re-runs the markdown
 * pipeline (react-markdown + remark-gfm) on every delta, so a long conversation
 * gets slower to stream into the longer it gets. The probe counts markdown
 * renders per content string: a history bubble's content must not be rendered
 * again while a reply streams in beside it.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";

const probe = vi.hoisted(() => ({ renders: new Map<string, number>() }));

vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => {
    probe.renders.set(children, (probe.renders.get(children) ?? 0) + 1);
    return <span>{children}</span>;
  },
}));

// Stable, as the real provider's callbacks are (they are useCallback'd).
const visual = vi.hoisted(() => ({
  notifyConversationUpdate: () => {},
  setRender: () => {},
}));
vi.mock("@/hooks/use-visual-state", () => ({ useVisualState: () => visual }));

// No throttle: every delta reaches the screen, the worst case the throttle hides.
vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (value: string) => value }));

import { ChatPanel } from "../chat-panel";

const HISTORY = [
  { role: "user", content: "What is on today?" },
  { role: "assistant", content: "Two meetings and **one** review." },
  { role: "user", content: "Summarise the review." },
];
const DELTAS = 20;

function streamOfDeltas(n: number) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    async start(controller) {
      for (let i = 0; i < n; i++) {
        controller.enqueue(encoder.encode(`event: delta\ndata: ${JSON.stringify({ text: `w${i} ` })}\n\n`));
        // Yield to the event loop so React commits between deltas, as it does live.
        await new Promise((r) => setTimeout(r, 0));
      }
      controller.enqueue(encoder.encode(`event: done\ndata: ${JSON.stringify({ status: "completed" })}\n\n`));
      controller.close();
    },
  });
}

describe("ChatPanel render cost of a streaming delta", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    probe.renders.clear();
  });

  it("does not re-render a history bubble's markdown while a reply streams", async () => {
    localStorage.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string | URL | Request) => {
        const u = String(url);
        if (u === "/api/chat/send") {
          return new Response(streamOfDeltas(DELTAS), { headers: { "content-type": "text/event-stream" } });
        }
        if (u.startsWith("/api/chat/history")) {
          return { ok: true, json: async () => ({ messages: HISTORY }) };
        }
        return { ok: true, json: async () => ({ messages: [], agents: [] }) };
      }),
    );

    render(<ChatPanel />);
    const historyAssistant = HISTORY[1].content;
    await waitFor(() => expect(probe.renders.get(historyAssistant)).toBeGreaterThan(0));
    const before = probe.renders.get(historyAssistant)!;

    await act(async () => {
      fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "Go on." } });
      fireEvent.click(screen.getByTestId("send-button"));
    });
    await waitFor(() => expect(screen.queryByTestId("streaming-message")).toBeNull());

    // The streamed reply did render, delta by delta: the probe is live.
    const streamedRenders = [...probe.renders.entries()]
      .filter(([content]) => content.startsWith("w0 "))
      .reduce((sum, [, count]) => sum + count, 0);
    expect(streamedRenders).toBeGreaterThan(1);

    const extra = probe.renders.get(historyAssistant)! - before;
    console.info(`[render-probe] history bubble markdown renders during ${DELTAS} deltas: ${extra}`);
    expect(extra).toBe(0);
  });
});
