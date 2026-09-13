import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ChatPanel } from "../chat-panel";

/**
 * `approval_required` reaching the browser. The engine has emitted this event
 * over the run's SSE stream since C10 and the panel dropped it on the floor —
 * an agent asked a question and the person it was asked of never saw it.
 */

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({
    notifyConversationUpdate: vi.fn(),
    setRender: vi.fn(),
  }),
}));

vi.mock("@/hooks/use-throttle", () => ({
  useThrottle: (value: string) => value,
}));

function makeAskSSE() {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          `event: approval_required\ndata: ${JSON.stringify({
            kind: "question",
            id: "q-42",
            run_id: "run-1",
            question: "Which vendor should I use?",
            options: ["Acme", "Globex"],
            expires_at: "2099-01-01T00:00:00+00:00",
          })}\n\n`
        )
      );
      controller.enqueue(
        encoder.encode(`event: done\ndata: ${JSON.stringify({ text: "Waiting on you." })}\n\n`)
      );
      controller.close();
    },
  });
}

function setupFetchMock() {
  global.fetch = vi.fn().mockImplementation(async (url: string) => {
    if (url === "/api/chat/history") return { ok: true, json: async () => ({ messages: [] }) };
    if (url === "/api/chat/plan/status") return { ok: true, json: async () => ({ active: false }) };
    if (url === "/api/chat/send") return { ok: true, body: makeAskSSE() };
    return { ok: true, json: async () => ({ settled: true }) };
  });
}

describe("ChatPanel — an agent's question", () => {
  beforeEach(() => {
    setupFetchMock();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("an approval_required SSE event renders the ask card", async () => {
    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("chat-input")).toBeTruthy();
    });

    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "pick a vendor for me" },
    });
    fireEvent.click(screen.getByTestId("send-button"));

    await waitFor(() => {
      expect(screen.getByTestId("ask-card")).toBeTruthy();
    });
    expect(screen.getByTestId("ask-card").textContent).toContain("Which vendor should I use?");
    expect(screen.getAllByTestId(/^ask-option-/)).toHaveLength(2);
  });
});
