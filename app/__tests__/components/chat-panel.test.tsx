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
            ])
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
