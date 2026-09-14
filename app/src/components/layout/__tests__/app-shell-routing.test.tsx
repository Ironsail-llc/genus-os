import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({
    notifyConversationUpdate: vi.fn(),
    setRender: vi.fn(),
    currentView: null,
    viewStack: [],
    popView: vi.fn(),
    clearViews: vi.fn(),
    canvasMode: "idle",
    setCanvasMode: vi.fn(),
    dashboardCode: null,
    setDashboardCode: vi.fn(),
    clearDashboard: vi.fn(),
    isUpdating: false,
    submitAction: vi.fn(),
    resolveAction: vi.fn(),
    pushView: vi.fn(),
  }),
}));

vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (v: string) => v }));

vi.mock("@/hooks/use-tasks", () => ({
  useTasks: () => ({
    tasks: [],
    isLoading: false,
    approveTask: vi.fn(),
    rejectTask: vi.fn(),
    answerQuestion: vi.fn(),
  }),
}));

vi.mock("@/hooks/use-agents", () => ({
  useAgents: () => ({
    agents: [],
    summary: { healthy: 3, degraded: 0, failed: 0, sleeping: 2, total: 5 },
    isLoading: false,
  }),
}));

vi.mock("@/hooks/use-dashboard-agent", () => ({ useDashboardAgent: vi.fn() }));
vi.mock("@/lib/api/health", () => ({ fetchHealth: vi.fn().mockResolvedValue({ status: "ok", services: [] }) }));
vi.mock("@/lib/api/people", () => ({ fetchPeople: vi.fn().mockResolvedValue([]) }));
vi.mock("@/lib/api/conversations", () => ({ fetchConversations: vi.fn().mockResolvedValue([]) }));
vi.mock("@/lib/api/memory", () => ({ searchMemory: vi.fn().mockResolvedValue([]) }));
vi.mock("cronstrue", () => ({ default: { toString: (expr: string) => expr } }));

const mockSession = vi.hoisted(() => ({
  role: "owner" as string | undefined,
  status: "authenticated" as "authenticated" | "loading" | "unauthenticated",
}));
vi.mock("next-auth/react", () => ({
  useSession: () => ({
    data: mockSession.status === "loading" ? undefined : { role: mockSession.role },
    status: mockSession.status,
    update: vi.fn(),
  }),
  SessionProvider: ({ children }: { children: React.ReactNode }) => children,
  signOut: vi.fn(),
  signIn: vi.fn(),
}));

function desktopViewport() {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1280 });
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query.includes("min-width: 1024px"),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

async function renderShell() {
  const { AppShell } = await import("../app-shell");
  return render(<AppShell />);
}

describe("AppShell — URL-synced views", () => {
  beforeEach(() => {
    mockSession.role = "owner";
    mockSession.status = "authenticated";
    desktopViewport();
    window.history.replaceState(null, "", "/");
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ messages: [], active: false }),
      text: () => Promise.resolve(JSON.stringify({ messages: [], active: false })),
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("defaults to chat on desktop", async () => {
    await renderShell();
    expect(screen.getByTestId("header-title").textContent).toBe("Chat");
    expect(screen.getByTestId("nav-chat")).toHaveAttribute("aria-current", "page");
  });

  it("writes the active view into the query string", async () => {
    await renderShell();
    fireEvent.click(screen.getByTestId("nav-runs"));
    expect(window.location.search).toBe("?v=runs");
    expect(screen.getByTestId("header-title").textContent).toBe("Runs");
  });

  it("lands on the view named in the URL", async () => {
    window.history.replaceState(null, "", "/?v=fleet");
    await renderShell();
    expect(screen.getByTestId("header-title").textContent).toBe("Fleet");
  });

  it("falls back to chat for an unknown view", async () => {
    window.history.replaceState(null, "", "/?v=bogus");
    await renderShell();
    expect(screen.getByTestId("header-title").textContent).toBe("Chat");
  });

  it("follows real back navigation", async () => {
    await renderShell();
    fireEvent.click(screen.getByTestId("nav-runs"));
    fireEvent.click(screen.getByTestId("nav-health"));
    expect(screen.getByTestId("header-title").textContent).toBe("Health");
    act(() => {
      window.history.back();
    });
    await waitFor(() =>
      expect(screen.getByTestId("header-title").textContent).toBe("Runs")
    );
  });

  it("does not stack history entries for the view already showing", async () => {
    await renderShell();
    fireEvent.click(screen.getByTestId("nav-runs"));
    const push = vi.spyOn(window.history, "pushState");
    fireEvent.click(screen.getByTestId("nav-runs"));
    fireEvent.click(screen.getByTestId("nav-runs"));
    expect(push).not.toHaveBeenCalled();
  });

  it("decides nothing about settings while the session is loading", async () => {
    mockSession.status = "loading";
    window.history.replaceState(null, "", "/?v=settings&s=flags");
    await renderShell();
    // The owner must not be told the screen is not theirs before the role is known.
    expect(screen.queryByTestId("settings-restricted")).toBeNull();
    expect(screen.getByTestId("settings-loading")).toBeInTheDocument();
  });

  it("round-trips a settings sub-page", async () => {
    window.history.replaceState(null, "", "/?v=settings&s=flags");
    await renderShell();
    expect(screen.getByTestId("header-title").textContent).toBe("Settings");
    expect(screen.getByTestId("settings-nav-flags")).toHaveAttribute("aria-current", "page");
  });

  it("shows the canvas as an optional rail on the chat view", async () => {
    await renderShell();
    expect(screen.queryByTestId("canvas-rail")).toBeNull();
    fireEvent.click(screen.getByTestId("canvas-rail-toggle"));
    expect(screen.getByTestId("canvas-rail")).toBeInTheDocument();
    // the rail belongs to chat only
    fireEvent.click(screen.getByTestId("nav-runs"));
    expect(screen.queryByTestId("canvas-rail")).toBeNull();
  });

  it("hides settings navigation from a non-operator", async () => {
    mockSession.role = "viewer";
    await renderShell();
    expect(screen.queryByTestId("nav-group-settings")).toBeNull();
  });

  it("shows the real Inbox at ?v=inbox rather than the coming-soon placeholder", async () => {
    window.history.replaceState(null, "", "/?v=inbox");
    await renderShell();
    expect(screen.getByTestId("header-title").textContent).toBe("Inbox");
    expect(screen.getByTestId("inbox-view")).toBeInTheDocument();
    expect(screen.queryByTestId("coming-soon-inbox")).toBeNull();
  });

  it("badges the sidebar Inbox from the same poll the view reads", async () => {
    // One hook in the shell feeds both, so a single GET has to serve them.
    const pending = {
      count: 1,
      pending: [
        {
          kind: "question",
          id: "11111111-1111-4111-8111-111111111111",
          run_id: "abcdef12-3456-4789-8abc-def012345678",
          agent_id: "invoice-chaser",
          question: "Which vendor should I chase first?",
          detail: "",
          options: [],
          expires_at: "2026-06-15T13:00:00Z",
          created_at: "2026-06-15T11:30:00Z",
        },
      ],
    };
    global.fetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : String(input);
      const body = url.includes("/api/approvals") ? pending : { messages: [], active: false };
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
        text: () => Promise.resolve(JSON.stringify(body)),
      });
    }) as unknown as typeof fetch;

    await renderShell();

    await waitFor(() => expect(screen.getByTestId("badge-inbox").textContent).toBe("1"));

    const approvalCalls = (global.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.filter(
      ([input]) => String(input).includes("/api/approvals")
    );
    expect(approvalCalls).toHaveLength(1);
  });
});
