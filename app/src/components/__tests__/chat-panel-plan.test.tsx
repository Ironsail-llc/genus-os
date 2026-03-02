import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
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

// Track fetch calls
let fetchCalls: { url: string; method?: string; body?: string }[] = [];

function makePlanSSE() {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          `event: plan\ndata: ${JSON.stringify({
            plan_id: "plan-1",
            plan_text: "1. Read inbox\n2. Create task",
            original_message: "check email",
            status: "pending",
          })}\n\n`
        )
      );
      controller.enqueue(
        encoder.encode(
          `event: done\ndata: ${JSON.stringify({
            text: "1. Read inbox\n2. Create task\n\n[PLAN_READY]",
            plan_id: "plan-1",
          })}\n\n`
        )
      );
      controller.close();
    },
  });
}

function makeApproveSSE() {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          `event: delta\ndata: ${JSON.stringify({ text: "Task created!" })}\n\n`
        )
      );
      controller.enqueue(
        encoder.encode(
          `event: done\ndata: ${JSON.stringify({ text: "Task created!" })}\n\n`
        )
      );
      controller.close();
    },
  });
}

function makeSendSSE(text = "Hello!") {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          `event: done\ndata: ${JSON.stringify({ text })}\n\n`
        )
      );
      controller.close();
    },
  });
}

function setupFetchMock(opts?: { planStatusActive?: boolean }) {
  fetchCalls = [];
  global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    fetchCalls.push({
      url,
      method: init?.method,
      body: init?.body as string | undefined,
    });

    if (url === "/api/chat/history") {
      return { ok: true, json: async () => ({ messages: [] }) };
    }

    if (url === "/api/chat/plan/status") {
      if (opts?.planStatusActive) {
        return {
          ok: true,
          json: async () => ({
            active: true,
            plan: {
              plan_id: "plan-recovered",
              plan_text: "Recovered plan text",
              original_message: "original msg",
              status: "pending",
            },
          }),
        };
      }
      return { ok: true, json: async () => ({ active: false }) };
    }

    if (url === "/api/chat/plan/start") {
      return { ok: true, body: makePlanSSE() };
    }

    if (url === "/api/chat/plan/approve") {
      return { ok: true, body: makeApproveSSE() };
    }

    if (url === "/api/chat/plan/reject") {
      return { ok: true, json: async () => ({ ok: true }) };
    }

    if (url === "/api/chat/send") {
      return { ok: true, body: makeSendSSE() };
    }

    return { ok: false, status: 404 };
  });
}

describe("ChatPanel — Plan Mode", () => {
  beforeEach(() => {
    setupFetchMock();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("plan toggle button renders and toggles amber styling", async () => {
    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });

    const toggle = screen.getByTestId("plan-toggle");
    expect(toggle.className).toContain("text-muted-foreground");

    // Toggle on
    fireEvent.click(toggle);
    expect(toggle.className).toContain("text-amber-400");

    // Badge appears
    expect(screen.getByTestId("plan-mode-badge")).toBeTruthy();
    expect(screen.getByTestId("plan-mode-badge").textContent).toBe("Plan Mode");

    // Placeholder changes
    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    expect(input.placeholder).toBe("Describe what you want planned...");

    // Toggle off
    fireEvent.click(toggle);
    expect(toggle.className).toContain("text-muted-foreground");
    expect(screen.queryByTestId("plan-mode-badge")).toBeNull();
    expect(input.placeholder).toBe("Ask me anything...");
  });

  it("plan mode routes message to /api/chat/plan/start", async () => {
    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });

    // Enable plan mode
    fireEvent.click(screen.getByTestId("plan-toggle"));

    // Type and send
    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "check email" } });
    fireEvent.click(screen.getByTestId("send-button"));

    // Verify plan/start was called, NOT chat/send
    await waitFor(() => {
      const planCall = fetchCalls.find((c) => c.url === "/api/chat/plan/start");
      expect(planCall).toBeTruthy();
      expect(planCall!.method).toBe("POST");
      const body = JSON.parse(planCall!.body!);
      expect(body.message).toBe("check email");
    });

    // Plan card should appear
    await waitFor(() => {
      expect(screen.getByTestId("plan-card")).toBeTruthy();
    });

    // chat/send should NOT have been called for this message
    const sendCalls = fetchCalls.filter((c) => c.url === "/api/chat/send");
    expect(sendCalls).toHaveLength(0);
  });

  it("plan card shows Approve, Edit, and Reject buttons", async () => {
    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });

    // Trigger plan mode flow
    fireEvent.click(screen.getByTestId("plan-toggle"));
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "check email" },
    });
    fireEvent.click(screen.getByTestId("send-button"));

    await waitFor(() => {
      expect(screen.getByTestId("plan-card")).toBeTruthy();
    });

    expect(screen.getByTestId("plan-approve")).toBeTruthy();
    expect(screen.getByTestId("plan-edit")).toBeTruthy();
    expect(screen.getByTestId("plan-reject")).toBeTruthy();
  });

  it("Edit button reveals feedback textarea", async () => {
    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });

    // Get plan card to show
    fireEvent.click(screen.getByTestId("plan-toggle"));
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "check email" },
    });
    fireEvent.click(screen.getByTestId("send-button"));

    await waitFor(() => {
      expect(screen.getByTestId("plan-card")).toBeTruthy();
    });

    // No feedback area initially
    expect(screen.queryByTestId("plan-feedback-area")).toBeNull();

    // Click Edit
    fireEvent.click(screen.getByTestId("plan-edit"));

    // Feedback area appears
    expect(screen.getByTestId("plan-feedback-area")).toBeTruthy();
    expect(screen.getByTestId("plan-feedback-input")).toBeTruthy();
    expect(screen.getByTestId("plan-revise")).toBeTruthy();

    // Revise button disabled when empty
    expect(screen.getByTestId("plan-revise")).toBeDisabled();

    // Type feedback — revise becomes enabled
    fireEvent.change(screen.getByTestId("plan-feedback-input"), {
      target: { value: "Add more detail" },
    });
    expect(screen.getByTestId("plan-revise")).not.toBeDisabled();
  });

  it("Revise sends feedback text (not original message) to plan/start", async () => {
    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });

    // Get plan card
    fireEvent.click(screen.getByTestId("plan-toggle"));
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "check email" },
    });
    fireEvent.click(screen.getByTestId("send-button"));

    await waitFor(() => {
      expect(screen.getByTestId("plan-card")).toBeTruthy();
    });

    // Click Edit, type feedback, click Revise
    fireEvent.click(screen.getByTestId("plan-edit"));
    fireEvent.change(screen.getByTestId("plan-feedback-input"), {
      target: { value: "Add error handling step" },
    });
    fireEvent.click(screen.getByTestId("plan-revise"));

    // Should NOT have called reject — supersede is implicit
    await waitFor(() => {
      const planCalls = fetchCalls.filter((c) => c.url === "/api/chat/plan/start");
      expect(planCalls.length).toBeGreaterThanOrEqual(2);
    });
    const rejectCalls = fetchCalls.filter((c) => c.url === "/api/chat/plan/reject");
    expect(rejectCalls).toHaveLength(0);

    // Should have re-triggered plan/start with the FEEDBACK text
    const planCalls = fetchCalls.filter((c) => c.url === "/api/chat/plan/start");
    const lastCall = planCalls[planCalls.length - 1];
    const body = JSON.parse(lastCall.body!);
    expect(body.message).toBe("Add error handling step");
  });

  it("plan status recovery on mount restores approval card", async () => {
    setupFetchMock({ planStatusActive: true });

    render(<ChatPanel />);

    // Plan card appears from status recovery (not from SSE)
    await waitFor(() => {
      expect(screen.getByTestId("plan-card")).toBeTruthy();
    });

    // Verify the recovered plan text
    expect(screen.getByTestId("plan-card").textContent).toContain(
      "Recovered plan text"
    );
  });

  it("keyboard shortcut Ctrl+Shift+P toggles plan mode", async () => {
    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });

    // Plan mode off initially
    expect(screen.queryByTestId("plan-mode-badge")).toBeNull();

    // Press Ctrl+Shift+P
    act(() => {
      window.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "P",
          ctrlKey: true,
          shiftKey: true,
          bubbles: true,
        })
      );
    });

    // Plan mode on
    expect(screen.getByTestId("plan-mode-badge")).toBeTruthy();

    // Press again — off
    act(() => {
      window.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "P",
          ctrlKey: true,
          shiftKey: true,
          bubbles: true,
        })
      );
    });

    expect(screen.queryByTestId("plan-mode-badge")).toBeNull();
  });

  it("planning indicator shows amber 'Exploring...' instead of typing dots", async () => {
    // Use a mock that delays so we can observe the planning state
    fetchCalls = [];
    let resolveStream: (() => void) | undefined;
    const streamPromise = new Promise<void>((r) => { resolveStream = r; });

    global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      fetchCalls.push({ url, method: init?.method, body: init?.body as string | undefined });

      if (url === "/api/chat/history") {
        return { ok: true, json: async () => ({ messages: [] }) };
      }
      if (url === "/api/chat/plan/status") {
        return { ok: true, json: async () => ({ active: false }) };
      }
      if (url === "/api/chat/plan/start") {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          async start(controller) {
            // Wait before sending events so we can observe the planning state
            await streamPromise;
            controller.enqueue(
              encoder.encode(
                `event: plan\ndata: ${JSON.stringify({
                  plan_id: "plan-1",
                  plan_text: "Step 1",
                  original_message: "test",
                  status: "pending",
                })}\n\n`
              )
            );
            controller.enqueue(
              encoder.encode(
                `event: done\ndata: ${JSON.stringify({ text: "Step 1" })}\n\n`
              )
            );
            controller.close();
          },
        });
        return { ok: true, body: stream };
      }
      return { ok: false, status: 404 };
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });

    // Enable plan mode & send
    fireEvent.click(screen.getByTestId("plan-toggle"));
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "test" },
    });
    fireEvent.click(screen.getByTestId("send-button"));

    // Planning indicator should appear
    await waitFor(() => {
      expect(screen.getByTestId("planning-indicator")).toBeTruthy();
    });
    expect(screen.getByTestId("planning-indicator").textContent).toContain(
      "Exploring..."
    );

    // Should NOT have typing dots
    expect(screen.queryByText((_, el) => el?.classList?.contains("typing-dot") ?? false)).toBeNull();

    // Resolve the stream to clean up
    resolveStream!();

    // After stream completes, plan card appears
    await waitFor(() => {
      expect(screen.getByTestId("plan-card")).toBeTruthy();
    });
  });

  it("plan mode badge persists after sending (not cleared on first send)", async () => {
    // Use a delayed stream so we can observe the badge during planning
    fetchCalls = [];
    let resolveStream: (() => void) | undefined;
    const streamPromise = new Promise<void>((r) => { resolveStream = r; });

    global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      fetchCalls.push({ url, method: init?.method, body: init?.body as string | undefined });
      if (url === "/api/chat/history") {
        return { ok: true, json: async () => ({ messages: [] }) };
      }
      if (url === "/api/chat/plan/status") {
        return { ok: true, json: async () => ({ active: false }) };
      }
      if (url === "/api/chat/plan/start") {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          async start(controller) {
            await streamPromise;
            controller.enqueue(
              encoder.encode(
                `event: done\ndata: ${JSON.stringify({ text: "A question for you" })}\n\n`
              )
            );
            controller.close();
          },
        });
        return { ok: true, body: stream };
      }
      return { ok: false, status: 404 };
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });

    // Enable plan mode
    fireEvent.click(screen.getByTestId("plan-toggle"));
    expect(screen.getByTestId("plan-mode-badge")).toBeTruthy();

    // Send a message
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "test" },
    });
    fireEvent.click(screen.getByTestId("send-button"));

    // Badge should still be visible during planning
    expect(screen.getByTestId("plan-mode-badge")).toBeTruthy();

    // Resolve and clean up
    resolveStream!();

    // After stream completes, badge should still be there (no gotPlanEvent)
    await waitFor(() => {
      expect(screen.getByTestId("plan-mode-badge")).toBeTruthy();
    });
  });

  it("non-PLAN_READY response keeps plan mode active for multi-turn", async () => {
    // Return a response without [PLAN_READY] (a question or clarification)
    fetchCalls = [];
    global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      fetchCalls.push({ url, method: init?.method, body: init?.body as string | undefined });
      if (url === "/api/chat/history") {
        return { ok: true, json: async () => ({ messages: [] }) };
      }
      if (url === "/api/chat/plan/status") {
        return { ok: true, json: async () => ({ active: false }) };
      }
      if (url === "/api/chat/plan/start") {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            // No plan event — just a done with text (agent asking a question)
            controller.enqueue(
              encoder.encode(
                `event: done\ndata: ${JSON.stringify({ text: "What kind of tasks?" })}\n\n`
              )
            );
            controller.close();
          },
        });
        return { ok: true, body: stream };
      }
      return { ok: false, status: 404 };
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("plan-toggle"));
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "check tasks" },
    });
    fireEvent.click(screen.getByTestId("send-button"));

    // Wait for response to complete
    await waitFor(() => {
      const msgs = screen.getAllByTestId("message-assistant");
      expect(msgs.length).toBeGreaterThanOrEqual(1);
    });

    // Plan mode badge should still be active (agent asked a question, no PLAN_READY)
    expect(screen.getByTestId("plan-mode-badge")).toBeTruthy();

    // No plan card should appear (no plan event)
    expect(screen.queryByTestId("plan-card")).toBeNull();
  });

  it("tool activity indicator shows tool name during tool_start events", async () => {
    fetchCalls = [];
    let resolveStream: (() => void) | undefined;
    const streamPromise = new Promise<void>((r) => { resolveStream = r; });

    global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      fetchCalls.push({ url, method: init?.method, body: init?.body as string | undefined });
      if (url === "/api/chat/history") {
        return { ok: true, json: async () => ({ messages: [] }) };
      }
      if (url === "/api/chat/plan/status") {
        return { ok: true, json: async () => ({ active: false }) };
      }
      if (url === "/api/chat/plan/start") {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          async start(controller) {
            // Send a tool_start event
            controller.enqueue(
              encoder.encode(
                `event: tool_start\ndata: ${JSON.stringify({ tool: "list_tasks" })}\n\n`
              )
            );
            // Wait so we can observe the state
            await streamPromise;
            controller.enqueue(
              encoder.encode(
                `event: tool_end\ndata: ${JSON.stringify({ tool: "list_tasks" })}\n\n`
              )
            );
            controller.enqueue(
              encoder.encode(
                `event: plan\ndata: ${JSON.stringify({
                  plan_id: "plan-t",
                  plan_text: "Tool plan",
                  original_message: "test",
                  status: "pending",
                })}\n\n`
              )
            );
            controller.enqueue(
              encoder.encode(
                `event: done\ndata: ${JSON.stringify({ text: "Tool plan" })}\n\n`
              )
            );
            controller.close();
          },
        });
        return { ok: true, body: stream };
      }
      return { ok: false, status: 404 };
    });

    render(<ChatPanel />);

    await waitFor(() => {
      expect(screen.getByTestId("plan-toggle")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("plan-toggle"));
    fireEvent.change(screen.getByTestId("chat-input"), {
      target: { value: "test" },
    });
    fireEvent.click(screen.getByTestId("send-button"));

    // Planning indicator should show tool name
    await waitFor(() => {
      expect(screen.getByTestId("planning-indicator")).toBeTruthy();
      expect(screen.getByTestId("planning-indicator").textContent).toContain(
        "Checking list_tasks..."
      );
    });

    // Resolve stream
    resolveStream!();

    // After completion, plan card should appear
    await waitFor(() => {
      expect(screen.getByTestId("plan-card")).toBeTruthy();
    });
  });
});
