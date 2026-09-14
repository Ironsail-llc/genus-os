import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

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

const mockRole = vi.hoisted(() => ({ value: "owner" as string | undefined }));
vi.mock("next-auth/react", () => ({
  useSession: () => ({ data: { role: mockRole.value }, status: "authenticated", update: vi.fn() }),
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
    mockRole.value = "owner";
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

  it("follows back navigation", async () => {
    await renderShell();
    fireEvent.click(screen.getByTestId("nav-health"));
    expect(screen.getByTestId("header-title").textContent).toBe("Health");
    act(() => {
      window.history.replaceState(null, "", "/?v=chat");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(screen.getByTestId("header-title").textContent).toBe("Chat");
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
    mockRole.value = "viewer";
    await renderShell();
    expect(screen.queryByTestId("nav-group-settings")).toBeNull();
  });
});
