import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ChatPanel } from "../chat-panel";

/**
 * `approval_required` reaching the browser, on **every** stream the panel reads.
 *
 * The engine has emitted this event over the run's SSE stream since C10 and the
 * panel dropped it on the floor. Wiring only the conversational `/chat/send`
 * stream would have been worse than nothing: `plan/approve` is the full-tools
 * execution run — `trigger_detail="plan-exec:…"`, the one Helm run where
 * `ask_user` actually fires — and with the webchat channel now waiting on the
 * row, a dropped event is a run blocked for up to 590s on a question nobody was
 * shown, reported to the model as "asked and stayed silent".
 *
 * So there is one test per stream, and each one goes red if that stream stops
 * calling the shared reducer.
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

const ASK = {
  kind: "question",
  id: "q-42",
  run_id: "run-1",
  question: "Which vendor should I use?",
  options: ["Acme", "Globex"],
  expires_at: "2099-01-01T00:00:00+00:00",
};

function sse(events: Array<{ event: string; data: Record<string, unknown> }>) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const e of events) {
        controller.enqueue(
          encoder.encode(`event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`),
        );
      }
      controller.close();
    },
  });
}

/** The ask event followed by the `done` every stream ends with. */
function askThenDone(extra: Array<{ event: string; data: Record<string, unknown> }> = []) {
  return sse([
    { event: "approval_required", data: ASK },
    ...extra,
    { event: "done", data: { text: "Waiting on you." } },
  ]);
}

interface StreamBodies {
  send?: () => ReadableStream<Uint8Array>;
  planStart?: () => ReadableStream<Uint8Array>;
  planApprove?: () => ReadableStream<Uint8Array>;
}

const PLAN_EVENT = {
  event: "plan",
  data: {
    plan_id: "plan-1",
    plan_text: "1. Pick a vendor",
    original_message: "sort out the vendor",
    status: "pending",
  },
};

function setupFetchMock(bodies: StreamBodies) {
  global.fetch = vi.fn().mockImplementation(async (url: string) => {
    if (url === "/api/chat/history") return { ok: true, json: async () => ({ messages: [] }) };
    if (url === "/api/chat/plan/status") return { ok: true, json: async () => ({ active: false }) };
    if (url === "/api/chat/deep/status") return { ok: true, json: async () => ({ active: false }) };
    if (url === "/api/chat/send") {
      return { ok: true, body: (bodies.send ?? askThenDone)() };
    }
    if (url === "/api/chat/plan/start") {
      return { ok: true, body: (bodies.planStart ?? (() => askThenDone([PLAN_EVENT])))() };
    }
    if (url === "/api/chat/plan/approve") {
      return { ok: true, body: (bodies.planApprove ?? askThenDone)() };
    }
    return { ok: true, json: async () => ({ settled: true }) };
  });
}

async function typeAndSend(text: string) {
  await waitFor(() => {
    expect(screen.getByTestId("chat-input")).toBeTruthy();
  });
  fireEvent.change(screen.getByTestId("chat-input"), { target: { value: text } });
  fireEvent.click(screen.getByTestId("send-button"));
}

async function expectAskCard() {
  await waitFor(() => {
    expect(screen.getByTestId("ask-card")).toBeTruthy();
  });
  expect(screen.getByTestId("ask-card").textContent).toContain("Which vendor should I use?");
  expect(screen.getAllByTestId(/^ask-option-/)).toHaveLength(2);
}

describe("ChatPanel — an agent's question, on every stream", () => {
  beforeEach(() => {
    setupFetchMock({});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the card from the /chat/send stream", async () => {
    render(<ChatPanel />);
    await typeAndSend("pick a vendor for me");
    await expectAskCard();
  });

  it("renders the card from the plan/start (exploration) stream", async () => {
    render(<ChatPanel />);
    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("plan-toggle"));
    await typeAndSend("sort out the vendor");
    await expectAskCard();
  });

  it("renders the card from the plan/approve (full-tools execution) stream", async () => {
    // The stream that matters most: `plan-exec:` is where `ask_user` fires, and
    // where a dropped event stalls the run for the whole tool budget.
    render(<ChatPanel />);
    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("plan-toggle"));
    setupFetchMock({ planStart: () => sse([PLAN_EVENT, { event: "done", data: { text: "" } }]) });
    await typeAndSend("sort out the vendor");

    await waitFor(() => {
      expect(screen.getByTestId("plan-card")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("plan-approve"));
    await expectAskCard();
  });

  it("renders the card from a deep-plan execution stream", async () => {
    render(<ChatPanel />);
    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("plan-toggle"));
    setupFetchMock({
      planStart: () =>
        sse([
          { event: "plan", data: { ...PLAN_EVENT.data, deep_plan: true } },
          { event: "done", data: { text: "" } },
        ]),
      planApprove: () =>
        askThenDone([
          { event: "deep_start", data: { deep_id: "d-1", query: "sort out the vendor" } },
        ]),
    });
    await typeAndSend("sort out the vendor");

    await waitFor(() => {
      expect(screen.getByTestId("plan-card")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("plan-approve"));
    await expectAskCard();
  });

  it("renders the card from the plan-revise stream", async () => {
    render(<ChatPanel />);
    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("plan-toggle"));
    setupFetchMock({ planStart: () => sse([PLAN_EVENT, { event: "done", data: { text: "" } }]) });
    await typeAndSend("sort out the vendor");

    await waitFor(() => {
      expect(screen.getByTestId("plan-card")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("plan-edit"));
    fireEvent.change(screen.getByTestId("plan-feedback-input"), {
      target: { value: "cheaper options only" },
    });
    // The revision goes back through plan/start, which now answers with an ask.
    setupFetchMock({});
    fireEvent.click(screen.getByTestId("plan-revise"));
    await expectAskCard();
  });

  it("ignores an approval_required event with no row id — there would be nothing to answer", async () => {
    setupFetchMock({
      send: () =>
        sse([
          { event: "approval_required", data: { kind: "question", question: "Which?" } },
          { event: "done", data: { text: "done" } },
        ]),
    });
    render(<ChatPanel />);
    await typeAndSend("pick a vendor for me");

    await waitFor(() => {
      expect(screen.queryByText("done")).toBeTruthy();
    });
    expect(screen.queryByTestId("ask-card")).toBeNull();
  });
});
