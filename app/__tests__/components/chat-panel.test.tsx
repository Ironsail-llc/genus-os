import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

// Mock react-markdown
vi.mock("react-markdown", () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));
vi.mock("remark-gfm", () => ({ default: () => {} }));

// Mock lucide-react
vi.mock("lucide-react", () => ({
  Send: () => <span data-testid="send-icon">Send</span>,
  Square: () => <span>Square</span>,
  Loader2: () => <span>Loading</span>,
}));

// Mock visual state
const mockNotifyConversationUpdate = vi.fn();
const mockSetRender = vi.fn();
vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({
    notifyConversationUpdate: mockNotifyConversationUpdate,
    setRender: mockSetRender,
    viewStack: [],
    currentView: null,
    pushView: vi.fn(),
    popView: vi.fn(),
    clearViews: vi.fn(),
    dashboardRequest: null,
    setDashboard: vi.fn(),
    clearDashboard: vi.fn(),
    renderRequest: null,
    canvasMode: "idle",
    setCanvasMode: vi.fn(),
    dashboardCode: null,
    dashboardCodeType: null,
    setDashboardCode: vi.fn(),
    clearDashboardCode: vi.fn(),
    isUpdating: false,
    setIsUpdating: vi.fn(),
    pendingMessages: null,
    pendingAgentData: null,
  }),
}));

// Mock fetch
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

import { ChatPanel } from "@/components/chat-panel";

describe("ChatPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: history returns empty
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ messages: [] }),
    });
  });

  it("renders chat panel container", () => {
    render(<ChatPanel />);
    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
  });

  it("displays initial greeting on empty state", () => {
    render(<ChatPanel />);
    expect(screen.getByTestId("empty-state")).toBeInTheDocument();
    expect(screen.getByText(/Hey Philip/)).toBeInTheDocument();
  });

  it("renders suggested prompts", () => {
    render(<ChatPanel />);
    const prompts = screen.getAllByTestId("suggested-prompt");
    expect(prompts.length).toBeGreaterThanOrEqual(3);
  });

  it("renders chat input with placeholder", () => {
    render(<ChatPanel />);
    const input = screen.getByTestId("chat-input");
    expect(input).toHaveAttribute("placeholder", "Ask me anything...");
  });

  it("renders send button", () => {
    render(<ChatPanel />);
    expect(screen.getByTestId("send-button")).toBeInTheDocument();
  });

  it("send button is disabled when input is empty", () => {
    render(<ChatPanel />);
    expect(screen.getByTestId("send-button")).toBeDisabled();
  });

  it("send button is enabled when input has text", () => {
    render(<ChatPanel />);
    const input = screen.getByTestId("chat-input");
    fireEvent.change(input, { target: { value: "hello" } });
    expect(screen.getByTestId("send-button")).not.toBeDisabled();
  });

  it("clicking suggested prompt fills input", () => {
    render(<ChatPanel />);
    const prompt = screen.getAllByTestId("suggested-prompt")[0];
    fireEvent.click(prompt);
    const input = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    expect(input.value).toBeTruthy();
  });

  it("loads history on mount", () => {
    render(<ChatPanel />);
    expect(mockFetch).toHaveBeenCalledWith("/api/chat/history");
  });

  it("renders Robothor header", () => {
    render(<ChatPanel />);
    expect(screen.getByText("Robothor")).toBeInTheDocument();
  });

  describe("conversation-driven dashboard", () => {
    function makeMockSSEResponse(events: Array<{ event: string; data: unknown }>) {
      const lines = events
        .map((ev) => `event: ${ev.event}\ndata: ${JSON.stringify(ev.data)}`)
        .join("\n\n") + "\n\n";

      const encoder = new TextEncoder();
      const encoded = encoder.encode(lines);
      let read = false;

      return {
        ok: true,
        body: {
          getReader() {
            return {
              read() {
                if (!read) {
                  read = true;
                  return Promise.resolve({ done: false, value: encoded });
                }
                return Promise.resolve({ done: true, value: undefined });
              },
            };
          },
        },
      };
    }

    /**
     * Simulate TCP chunk splitting: SSE data arrives in multiple read() calls.
     * Splits between "event:" and "data:" lines to test the parser handles this.
     */
    function makeSplitChunkSSEResponse(events: Array<{ event: string; data: unknown }>) {
      const encoder = new TextEncoder();
      // Build each event as a separate chunk (simulates chunk split between event: and data: lines)
      const chunks: Uint8Array[] = events.map((ev) =>
        encoder.encode(`event: ${ev.event}\ndata: ${JSON.stringify(ev.data)}\n\n`)
      );
      // Further split the first chunk between the event: line and data: line
      if (chunks.length > 0) {
        const firstText = new TextDecoder().decode(chunks[0]);
        const newlineIdx = firstText.indexOf("\n");
        if (newlineIdx > 0) {
          const part1 = encoder.encode(firstText.substring(0, newlineIdx + 1));
          const part2 = encoder.encode(firstText.substring(newlineIdx + 1));
          chunks.splice(0, 1, part1, part2);
        }
      }
      let readIdx = 0;
      return {
        ok: true,
        body: {
          getReader() {
            return {
              read() {
                if (readIdx < chunks.length) {
                  const value = chunks[readIdx++];
                  return Promise.resolve({ done: false, value });
                }
                return Promise.resolve({ done: true, value: undefined });
              },
            };
          },
        },
      };
    }

    it("calls notifyConversationUpdate with recent messages after assistant response", async () => {
      let callCount = 0;
      mockFetch.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ messages: [] }),
          });
        }
        return Promise.resolve(
          makeMockSSEResponse([
            { event: "delta", data: { text: "Here are your contacts." } },
            { event: "dashboard", data: { intent: "contacts", data: {} } },
            { event: "done", data: { text: "Here are your contacts." } },
          ])
        );
      });

      const { getByTestId } = render(<ChatPanel />);

      // Wait for history fetch
      await vi.waitFor(() => expect(callCount).toBe(1), { timeout: 1000 });

      const input = getByTestId("chat-input");
      fireEvent.change(input, { target: { value: "Show contacts" } });
      fireEvent.click(getByTestId("send-button"));

      await vi.waitFor(
        () => {
          expect(mockNotifyConversationUpdate).toHaveBeenCalledWith(
            expect.arrayContaining([
              expect.objectContaining({ role: "user", content: "Show contacts" }),
            ]),
            undefined // empty dashboard data object is passed as undefined
          );
        },
        { timeout: 3000 }
      );
    });

    it("passes agent data from dashboard markers to notifyConversationUpdate", async () => {
      let callCount = 0;
      mockFetch.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ messages: [] }),
          });
        }
        return Promise.resolve(
          makeMockSSEResponse([
            { event: "delta", data: { text: "The weather in NYC is sunny." } },
            {
              event: "dashboard",
              data: {
                intent: "weather",
                data: { web: { query: "weather NYC", results: [{ title: "NYC Weather", snippet: "Sunny, 72F" }] } },
              },
            },
            { event: "done", data: { text: "The weather in NYC is sunny." } },
          ])
        );
      });

      const { getByTestId } = render(<ChatPanel />);
      await vi.waitFor(() => expect(callCount).toBe(1), { timeout: 1000 });

      const input = getByTestId("chat-input");
      fireEvent.change(input, { target: { value: "What's the weather?" } });
      fireEvent.click(getByTestId("send-button"));

      await vi.waitFor(
        () => {
          expect(mockNotifyConversationUpdate).toHaveBeenCalledWith(
            expect.arrayContaining([
              expect.objectContaining({ role: "user", content: "What's the weather?" }),
            ]),
            expect.objectContaining({
              web: expect.objectContaining({ query: "weather NYC" }),
            })
          );
        },
        { timeout: 3000 }
      );
    });

    it("notifies conversation update even without dashboard markers", async () => {
      let callCount = 0;
      mockFetch.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ messages: [] }),
          });
        }
        return Promise.resolve(
          makeMockSSEResponse([
            { event: "delta", data: { text: "The bridge is healthy." } },
            { event: "done", data: { text: "The bridge is healthy." } },
          ])
        );
      });

      const { getByTestId } = render(<ChatPanel />);
      await vi.waitFor(() => expect(callCount).toBe(1), { timeout: 1000 });

      const input = getByTestId("chat-input");
      fireEvent.change(input, { target: { value: "Check services" } });
      fireEvent.click(getByTestId("send-button"));

      await vi.waitFor(
        () => {
          expect(mockNotifyConversationUpdate).toHaveBeenCalled();
        },
        { timeout: 3000 }
      );
    });

    it("handles SSE chunk splitting without duplicating messages", async () => {
      let callCount = 0;
      mockFetch.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ messages: [] }),
          });
        }
        // Use split chunks — event: and data: lines arrive in separate read() calls
        return Promise.resolve(
          makeSplitChunkSSEResponse([
            { event: "delta", data: { text: "Hello Philip." } },
            { event: "done", data: { text: "Hello Philip." } },
          ])
        );
      });

      const { getByTestId, getAllByTestId } = render(<ChatPanel />);
      await vi.waitFor(() => expect(callCount).toBe(1), { timeout: 1000 });

      const input = getByTestId("chat-input");
      fireEvent.change(input, { target: { value: "Hi" } });
      fireEvent.click(getByTestId("send-button"));

      await vi.waitFor(
        () => {
          const assistantMsgs = getAllByTestId("message-assistant");
          expect(assistantMsgs).toHaveLength(1);
          // Should NOT contain duplicated text
          expect(assistantMsgs[0].textContent).toBe("Hello Philip.");
        },
        { timeout: 3000 }
      );
    });

    it("strips residual markers from final assistant message", async () => {
      let callCount = 0;
      mockFetch.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ messages: [] }),
          });
        }
        // Simulate marker text leaking through the interceptor
        return Promise.resolve(
          makeMockSSEResponse([
            { event: "delta", data: { text: 'On it.[DASHBOARD:{"intent":"health"}]' } },
            { event: "done", data: { text: 'On it.[DASHBOARD:{"intent":"health"}]' } },
          ])
        );
      });

      const { getByTestId, getAllByTestId } = render(<ChatPanel />);
      await vi.waitFor(() => expect(callCount).toBe(1), { timeout: 1000 });

      const input = getByTestId("chat-input");
      fireEvent.change(input, { target: { value: "Check health" } });
      fireEvent.click(getByTestId("send-button"));

      await vi.waitFor(
        () => {
          const assistantMsgs = getAllByTestId("message-assistant");
          expect(assistantMsgs).toHaveLength(1);
          // Marker text should be stripped from final message
          expect(assistantMsgs[0].textContent).toBe("On it.");
        },
        { timeout: 3000 }
      );
    });

    it("RENDER markers still trigger setRender immediately", async () => {
      let callCount = 0;
      mockFetch.mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ messages: [] }),
          });
        }
        return Promise.resolve(
          makeMockSSEResponse([
            { event: "delta", data: { text: "Rendering component." } },
            {
              event: "render",
              data: { component: "contacts_list", props: { limit: 5 } },
            },
            { event: "done", data: { text: "Rendering component." } },
          ])
        );
      });

      const { getByTestId } = render(<ChatPanel />);
      await vi.waitFor(() => expect(callCount).toBe(1), { timeout: 1000 });

      const input = getByTestId("chat-input");
      fireEvent.change(input, { target: { value: "Render contacts" } });
      fireEvent.click(getByTestId("send-button"));

      await vi.waitFor(
        () => {
          expect(mockSetRender).toHaveBeenCalledWith(
            expect.objectContaining({
              component: "contacts_list",
              props: { limit: 5 },
            })
          );
        },
        { timeout: 3000 }
      );
    });
  });
});
