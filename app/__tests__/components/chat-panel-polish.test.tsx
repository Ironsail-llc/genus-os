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
  Check: () => <span>Check</span>,
  X: () => <span>X</span>,
  ClipboardList: () => <span>ClipboardList</span>,
  MessageSquareText: () => <span>MessageSquareText</span>,
  Brain: () => <span>Brain</span>,
  PanelRightClose: () => <span>PanelRightClose</span>,
}));

// Mock UI components that use radix-ui internally
vi.mock("@/components/ui/badge", () => ({
  Badge: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => (
    <span {...props}>{children}</span>
  ),
}));

vi.mock("@/components/ui/tooltip", () => ({
  TooltipProvider: ({ children }: React.PropsWithChildren) => <>{children}</>,
  Tooltip: ({ children }: React.PropsWithChildren) => <>{children}</>,
  TooltipTrigger: ({ children }: React.PropsWithChildren) => <>{children}</>,
  TooltipContent: ({ children }: React.PropsWithChildren) => <span>{children}</span>,
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

// The landing has its own tests; here only where ChatPanel shows it matters.
vi.mock("@/components/chat/chat-landing", () => ({
  ChatLanding: ({
    ownerName,
    suggestedPrompts,
    onPrompt,
    onViewOpened,
  }: {
    ownerName?: string;
    suggestedPrompts: string[];
    onPrompt: (prompt: string) => void;
    onViewOpened?: () => void;
  }) => (
    <div
      data-testid="chat-landing"
      data-owner={ownerName ?? ""}
      data-prompts={JSON.stringify(suggestedPrompts)}
    >
      <button data-testid="landing-prompt" onClick={() => onPrompt(suggestedPrompts[0])}>
        {suggestedPrompts[0]}
      </button>
      <button data-testid="landing-view-opened" onClick={() => onViewOpened?.()}>
        opened
      </button>
    </div>
  ),
}));

// Mock fetch
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

import { ChatPanel } from "@/components/chat-panel";

/**
 * Chat polish, wired into the panel: real history times, "Load earlier",
 * Regenerate and Edit-and-resend. The parts have their own tests; these prove
 * the panel hands them the right messages and sends what it should.
 */

const T = (min: number) => `2026-10-31T09:${String(min).padStart(2, "0")}:00Z`;

function sseResponse(events: Array<{ event: string; data: unknown }>) {
  const encoded = new TextEncoder().encode(
    events.map((ev) => `event: ${ev.event}\ndata: ${JSON.stringify(ev.data)}`).join("\n\n") + "\n\n",
  );
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
          cancel() {
            return Promise.resolve();
          },
          releaseLock() {},
        };
      },
    },
  };
}

type Row = { role: string; content: string; id?: number; timestamp?: string };

/** Route the panel's requests: history pages by cursor, every send answered "Again." */
function serve(first: { messages: Row[]; hasMore?: boolean }, older: Record<number, { messages: Row[]; hasMore: boolean }> = {}) {
  const sends: Array<Record<string, unknown>> = [];
  mockFetch.mockImplementation((url: string, init?: { method?: string; body?: string }) => {
    if (url.startsWith("/api/chat/send")) {
      sends.push(JSON.parse(init?.body || "{}"));
      return Promise.resolve(sseResponse([{ event: "done", data: { text: "Again." } }]));
    }
    if (url.startsWith("/api/chat/history")) {
      const before = new URL(url, "http://x").searchParams.get("before");
      const body = before ? older[Number(before)] : first;
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ active: false }) });
  });
  return sends;
}

async function bubbles() {
  return screen.findAllByTestId(/^message-(user|assistant)$/);
}

describe("ChatPanel polish", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows each history message's stored time, not the time it loaded", async () => {
    serve({
      messages: [
        { role: "user", content: "hi", id: 1, timestamp: T(0) },
        { role: "assistant", content: "hello", id: 2, timestamp: T(1) },
      ],
    });
    const { container } = render(<ChatPanel />);
    await screen.findByText("hello");
    const times = [...container.querySelectorAll("time")].map((t) => t.getAttribute("datetime"));
    expect(times).toEqual(["2026-10-31T09:00:00.000Z", "2026-10-31T09:01:00.000Z"]);
  });

  it("shows no time on a history that carries none", async () => {
    serve({ messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "hello" }] });
    const { container } = render(<ChatPanel />);
    await screen.findByText("hello");
    expect(container.querySelectorAll("time")).toHaveLength(0);
  });

  it("loads earlier messages above the ones on screen, then stops offering", async () => {
    serve(
      { messages: [{ role: "user", content: "newer", id: 3, timestamp: T(3) }], hasMore: true },
      { 3: { messages: [{ role: "user", content: "older", id: 1, timestamp: T(1) }, { role: "assistant", content: "old reply", id: 2, timestamp: T(2) }], hasMore: false } },
    );
    render(<ChatPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /load earlier/i }));
    await screen.findByText("older");
    expect((await bubbles()).map((b) => b.textContent)).toEqual([
      expect.stringContaining("older"),
      expect.stringContaining("old reply"),
      expect.stringContaining("newer"),
    ]);
    expect(screen.queryByRole("button", { name: /load earlier/i })).toBeNull();
  });

  it("offers no Load earlier when the history says there is nothing more", async () => {
    serve({ messages: [{ role: "user", content: "hi", id: 1 }], hasMore: false });
    render(<ChatPanel />);
    await screen.findByText("hi");
    expect(screen.queryByRole("button", { name: /load earlier/i })).toBeNull();
  });

  it("regenerates by re-sending the user turn before the reply, once", async () => {
    const sends = serve({
      messages: [
        { role: "user", content: "first question", id: 1 },
        { role: "assistant", content: "first answer", id: 2 },
        { role: "user", content: "second question", id: 3 },
        { role: "assistant", content: "second answer", id: 4 },
      ],
    });
    render(<ChatPanel />);
    await screen.findByText("second answer");
    const regen = screen.getAllByRole("button", { name: "Regenerate response" });
    fireEvent.click(regen[0]);
    await vi.waitFor(() => expect(sends).toHaveLength(1));
    expect(sends[0].message).toBe("first question");
    await screen.findByText("Again.");
  });

  it("edit puts the text back in the composer and drops it and what followed from view", async () => {
    const sends = serve({
      messages: [
        { role: "user", content: "keep me", id: 1 },
        { role: "assistant", content: "kept reply", id: 2 },
        { role: "user", content: "fix me", id: 3 },
        { role: "assistant", content: "dropped reply", id: 4 },
      ],
    });
    render(<ChatPanel />);
    await screen.findByText("dropped reply");
    const edits = screen.getAllByRole("button", { name: "Edit message" });
    fireEvent.click(edits[1]);
    expect(screen.getByTestId("chat-input")).toHaveValue("fix me");
    expect(screen.queryByText("dropped reply")).toBeNull();
    expect(screen.queryByText("fix me")).toBeNull();
    expect(screen.getByText("kept reply")).toBeInTheDocument();
    expect(sends).toHaveLength(0);
  });
});
