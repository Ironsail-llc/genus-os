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


/**
 * The escalation half of the same event.
 *
 * `permission_escalation._announce` emits `approval_required` with
 * `kind: "escalation"` and a `tool`, and the coroutine that is waiting lives in
 * the ENGINE with a hard timeout. The panel has to carry `kind`, `tool` and
 * `timeout_seconds` through to the card, because the card is what decides which
 * route the answer goes to — and an escalation sent to the question route is an
 * answer that reaches nobody while the agent waits out its whole budget.
 */
const ESCALATION_EVENT = {
  event: "approval_required",
  data: {
    kind: "escalation",
    id: "esc-9",
    run_id: "run-1",
    agent_id: "scheduler",
    tool: "exec",
    question: "Agent scheduler is requesting approval.\nTool: exec",
    options: ["Approve", "Approve All", "Deny"],
    timeout_seconds: 90,
  },
};

describe("ChatPanel — a tool-permission escalation on the stream", () => {
  beforeEach(() => {
    setupFetchMock({
      send: () =>
        sse([ESCALATION_EVENT, { event: "done", data: { text: "Waiting on you." } }]),
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the escalation card, with its tool and its countdown", async () => {
    render(<ChatPanel />);
    await typeAndSend("tidy the logs");

    await waitFor(() => {
      expect(screen.getByTestId("escalation-card")).toBeTruthy();
    });
    expect(screen.getByTestId("escalation-tool").textContent).toContain("exec");
    expect(screen.getByTestId("escalation-countdown").textContent).toContain("90s");
  });

  it("answers it at the escalation route, not the question route", async () => {
    render(<ChatPanel />);
    await typeAndSend("tidy the logs");
    await waitFor(() => {
      expect(screen.getByTestId("escalation-allow-once")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("escalation-allow-once"));

    await waitFor(() => {
      const posted = (global.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map(
        (call) => String(call[0]),
      );
      expect(posted).toContain("/api/bridge/api/approvals/escalation/esc-9");
    });
  });

  it("shows one card per id, however many times the event arrives", async () => {
    setupFetchMock({
      send: () =>
        sse([
          ESCALATION_EVENT,
          ESCALATION_EVENT,
          { event: "done", data: { text: "Waiting on you." } },
        ]),
    });
    render(<ChatPanel />);
    await typeAndSend("tidy the logs");

    await waitFor(() => {
      expect(screen.getAllByTestId("escalation-card")).toHaveLength(1);
    });
  });

  it("shows BOTH escalations when a run raises two — each holds a blocked coroutine", async () => {
    // Probe C3 from the hostile review. A single `activeAsk` slot meant the
    // second frame overwrote the first, and the overwritten one is not a card
    // the operator can scroll back to: it is a coroutine blocked in the engine
    // that now runs out its whole timeout and denies. Unlike a durable
    // question, nothing recovers it.
    const second = {
      ...ESCALATION_EVENT,
      data: { ...ESCALATION_EVENT.data, id: "esc-10", tool: "web_fetch" },
    };
    setupFetchMock({
      send: () =>
        sse([ESCALATION_EVENT, second, { event: "done", data: { text: "Waiting on you." } }]),
    });
    render(<ChatPanel />);
    await typeAndSend("tidy the logs");

    await waitFor(() => {
      expect(screen.getAllByTestId("escalation-card")).toHaveLength(2);
    });
    expect(screen.getAllByTestId("escalation-tool").map((el) => el.textContent)).toEqual([
      "exec",
      "web_fetch",
    ]);
  });

  it("answers each of two escalations at its own id", async () => {
    const second = {
      ...ESCALATION_EVENT,
      data: { ...ESCALATION_EVENT.data, id: "esc-10", tool: "web_fetch" },
    };
    setupFetchMock({
      send: () =>
        sse([ESCALATION_EVENT, second, { event: "done", data: { text: "Waiting on you." } }]),
    });
    render(<ChatPanel />);
    await typeAndSend("tidy the logs");
    await waitFor(() => {
      expect(screen.getAllByTestId("escalation-allow-once")).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByTestId("escalation-deny")[1]);

    await waitFor(() => {
      const posted = (global.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map(
        (call) => String(call[0]),
      );
      expect(posted).toContain("/api/bridge/api/approvals/escalation/esc-10");
    });
    // The first card is untouched: one answer settles one decision.
    expect(screen.getAllByTestId("escalation-allow-once")[0]).toHaveProperty("disabled", false);
  });

  it("dismisses a settled card when the person sends the next message", async () => {
    render(<ChatPanel />);
    await typeAndSend("tidy the logs");
    await waitFor(() => {
      expect(screen.getByTestId("escalation-card")).toBeTruthy();
    });

    setupFetchMock({ send: () => sse([{ event: "done", data: { text: "Done." } }]) });
    await typeAndSend("never mind");

    await waitFor(() => {
      expect(screen.queryByTestId("escalation-card")).toBeNull();
    });
  });
});
