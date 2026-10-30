import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

// Mock hooks
const mockUseTasks = vi.fn();
const mockUseAgents = vi.fn();

vi.mock("@/hooks/use-tasks", () => ({
  useTasks: (...args: unknown[]) => mockUseTasks(...args),
}));

vi.mock("@/hooks/use-agents", () => ({
  useAgents: () => mockUseAgents(),
}));

// Mock next/image
vi.mock("next/image", () => ({
  default: (props: Record<string, unknown>) => <img alt="" {...props} />,
}));

// Mock shadcn tooltip
vi.mock("@/components/ui/tooltip", () => ({
  Tooltip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipContent: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

// Mock child components to keep tests focused on AppShell logic
vi.mock("@/components/canvas/live-canvas", () => ({
  LiveCanvas: () => <div data-testid="live-canvas">LiveCanvas</div>,
}));

// The real panel draws its own collapse button when told it is docked; the
// stand-in does the same so these tests exercise the shell's wiring.
vi.mock("@/components/chat-panel", () => ({
  ChatPanel: ({
    variant,
    ownerName,
    docked,
    onCollapse,
  }: {
    variant?: string;
    ownerName?: string;
    docked?: boolean;
    onCollapse?: () => void;
  }) => (
    <div data-testid="chat-panel" data-variant={variant ?? ""} data-owner={ownerName ?? ""}>
      ChatPanel
      {docked && (
        <button data-testid="chat-dock-close" aria-label="Collapse chat panel" onClick={onCollapse} />
      )}
    </div>
  ),
}));

// The session the shell reads the owner's name from; no session by default.
const mockUseSession = vi.fn();
vi.mock("next-auth/react", () => ({
  useSession: () => mockUseSession(),
  SessionProvider: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock("@/components/business/task-board", () => ({
  TaskBoard: ({ tasks }: { tasks: unknown[] }) => (
    <div data-testid="task-board">{tasks.length} tasks</div>
  ),
}));

vi.mock("@/components/business/agent-status", () => ({
  AgentStatus: ({ agents }: { agents: unknown[] }) => (
    <div data-testid="agent-status">{agents.length} agents</div>
  ),
}));

vi.mock("@/components/business/metric-grid", () => ({
  MetricGrid: () => <div data-testid="metric-grid">Metrics</div>,
}));

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({
    currentView: null,
    viewStack: [],
    popView: vi.fn(),
    clearViews: vi.fn(),
    canvasMode: "idle",
    setCanvasMode: vi.fn(),
    dashboardCode: null,
    dashboardCodeType: null,
    setDashboardCode: vi.fn(),
    clearDashboard: vi.fn(),
    isUpdating: false,
    submitAction: vi.fn(),
    resolveAction: vi.fn(),
  }),
  VisualStateProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@/hooks/use-dashboard-agent", () => ({
  useDashboardAgent: vi.fn(),
}));

vi.mock("@/lib/event-bus/use-event-stream", () => ({
  useEventStream: () => ({ events: [] }),
}));

vi.mock("@/hooks/use-mobile", () => ({
  useScreenSize: () => "desktop",
  useIsMobile: () => false,
}));

import { AppShell } from "@/components/layout/app-shell";

describe("AppShell", () => {
  beforeEach(() => {
    // The active view lives in the query string now — reset it between tests.
    window.history.replaceState(null, "", "/");
    mockUseSession.mockReturnValue({ data: null, status: "unauthenticated", update: vi.fn() });
    // The dock's open state is remembered per browser — start every test fresh.
    window.localStorage.clear();
    mockUseTasks.mockReturnValue({
      tasks: [
        { id: "1", title: "Test Task", status: "REVIEW" },
        { id: "2", title: "Task 2", status: "TODO" },
      ],
      isLoading: false,
      refetch: vi.fn(),
      updateTaskStatus: vi.fn(),
      approveTask: vi.fn(),
      rejectTask: vi.fn(),
    });
    mockUseAgents.mockReturnValue({
      agents: [
        { name: "classifier", status: "healthy", schedule: "0 * * * *" },
        { name: "vision", status: "failed", schedule: "*/10 * * * *" },
      ],
      summary: { healthy: 1, degraded: 0, failed: 1, sleeping: 0, total: 2 },
      isLoading: false,
      refetch: vi.fn(),
    });
  });

  it("renders the sidebar", () => {
    render(<AppShell />);
    expect(screen.getByTestId("sidebar")).toBeInTheDocument();
  });

  it("renders the header bar", () => {
    render(<AppShell />);
    expect(screen.getByTestId("header-bar")).toBeInTheDocument();
    // brand lockup lives in the sidebar since the B3 chrome rework
    expect(screen.getByTestId("sidebar-bolt")).toHaveTextContent(/Genus\s*OS/);
  });

  it("header shows current view title", () => {
    render(<AppShell />);
    expect(screen.getByTestId("header-title")).toHaveTextContent("Chat");
  });

  it("header title updates when view changes", () => {
    render(<AppShell />);
    fireEvent.click(screen.getByTestId("nav-tasks"));
    expect(screen.getByTestId("header-title")).toHaveTextContent("Tasks");
  });

  it("header shows system status dot", () => {
    render(<AppShell />);
    const dot = screen.getByTestId("system-status-dot");
    expect(dot).toBeInTheDocument();
    // 1 failed agent -> warning-token dot
    expect(dot.className).toContain("bg-warning");
  });

  it("renders chat panel", () => {
    render(<AppShell />);
    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
  });

  it("defaults to the chat view, with the old dashboard one click away", () => {
    render(<AppShell />);
    expect(screen.getByTestId("header-title")).toHaveTextContent("Chat");
    expect(screen.getByTestId("dashboard-view").style.display).toBe("none");
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    expect(screen.getByTestId("dashboard-view").style.display).toBe("flex");
    expect(screen.getByTestId("tasks-view").style.display).toBe("none");
  });

  it("centres the chat home stage at a readable width", () => {
    render(<AppShell />);
    const stage = screen.getByTestId("chat-container");
    const wrapper = stage.querySelector(".max-w-5xl");
    expect(wrapper).not.toBeNull();
    expect(wrapper!.className).toContain("mx-auto");
    expect(wrapper!.className).toContain("w-full");
    expect(wrapper!.contains(screen.getByTestId("chat-panel"))).toBe(true);
  });

  it("does not cap the docked chat beside another view", () => {
    render(<AppShell />);
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    expect(screen.getByTestId("chat-container").querySelector(".max-w-5xl")).toBeNull();
  });

  it("tells the chat panel it is the page on the chat home and the dock elsewhere", () => {
    render(<AppShell />);
    expect(screen.getByTestId("chat-panel")).toHaveAttribute("data-variant", "page");
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    expect(screen.getByTestId("chat-panel")).toHaveAttribute("data-variant", "dock");
  });

  it("passes the owner's first name from the session to the chat panel", () => {
    mockUseSession.mockReturnValue({
      data: { user: { name: "Alice Example" } },
      status: "authenticated",
      update: vi.fn(),
    });
    render(<AppShell />);
    expect(screen.getByTestId("chat-panel")).toHaveAttribute("data-owner", "Alice");
  });

  it("passes no owner name when the session has none", () => {
    mockUseSession.mockReturnValue({
      data: { user: { email: "agent@example.com" } },
      status: "authenticated",
      update: vi.fn(),
    });
    render(<AppShell />);
    expect(screen.getByTestId("chat-panel")).toHaveAttribute("data-owner", "");
  });

  it("switches to tasks view", () => {
    render(<AppShell />);
    fireEvent.click(screen.getByTestId("nav-tasks"));
    expect(screen.getByTestId("tasks-view").style.display).toBe("flex");
    expect(screen.getByTestId("dashboard-view").style.display).toBe("none");
  });

  it("switches to agents view", () => {
    render(<AppShell />);
    fireEvent.click(screen.getByTestId("nav-agents"));
    expect(screen.getByTestId("agents-view").style.display).toBe("flex");
  });

  it("toggles the docked chat panel beside another view", () => {
    render(<AppShell />);
    // The docked width only applies off the chat view, which owns the column.
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    const chatContainer = screen.getByTestId("chat-container");
    // Initially open (400px)
    expect(chatContainer.style.width).toBe("400px");
    fireEvent.click(screen.getByTestId("chat-panel-toggle"));
    expect(chatContainer.style.width).toBe("0px");
    fireEvent.click(screen.getByTestId("chat-panel-toggle"));
    expect(chatContainer.style.width).toBe("400px");
  });

  it("collapses the docked chat from the panel's own header", () => {
    render(<AppShell />);
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    const chatContainer = screen.getByTestId("chat-container");
    expect(chatContainer.style.width).toBe("400px");
    fireEvent.click(screen.getByTestId("chat-dock-close"));
    expect(chatContainer.style.width).toBe("0px");
    // The sidebar toggle reports the same state.
    expect(screen.getByTestId("chat-panel-toggle")).toHaveAttribute("aria-pressed", "false");
  });

  it("reopens a collapsed dock from the tab at the right edge", () => {
    render(<AppShell />);
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    expect(screen.queryByTestId("chat-dock-open")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("chat-dock-close"));
    const tab = screen.getByTestId("chat-dock-open");
    expect(tab).toHaveAttribute("aria-label", "Open chat panel");
    fireEvent.click(tab);
    expect(screen.getByTestId("chat-container").style.width).toBe("400px");
    expect(screen.getByTestId("chat-panel-toggle")).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByTestId("chat-dock-open")).not.toBeInTheDocument();
  });

  it("remembers a collapse and a reopen across loads", () => {
    render(<AppShell />);
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    fireEvent.click(screen.getByTestId("chat-dock-close"));
    expect(window.localStorage.getItem("genus.chatDockOpen")).toBe("false");
    fireEvent.click(screen.getByTestId("chat-dock-open"));
    expect(window.localStorage.getItem("genus.chatDockOpen")).toBe("true");
    fireEvent.click(screen.getByTestId("chat-panel-toggle"));
    expect(window.localStorage.getItem("genus.chatDockOpen")).toBe("false");
  });

  it("starts collapsed when the browser remembered a collapse", () => {
    window.localStorage.setItem("genus.chatDockOpen", "false");
    render(<AppShell />);
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    expect(screen.getByTestId("chat-container").style.width).toBe("0px");
    expect(screen.getByTestId("chat-panel-toggle")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("chat-dock-open")).toBeInTheDocument();
  });

  it("follows a collapse made in another tab when it is written", () => {
    render(<AppShell />);
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    const chatContainer = screen.getByTestId("chat-container");
    expect(chatContainer.style.width).toBe("400px");
    // Another tab writes the key; this tab hears it through the storage event.
    window.localStorage.setItem("genus.chatDockOpen", "false");
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: "genus.chatDockOpen", newValue: "false" }),
      );
    });
    expect(chatContainer.style.width).toBe("0px");
    expect(screen.getByTestId("chat-panel-toggle")).toHaveAttribute("aria-pressed", "false");
  });

  it("ignores another tab's write to a different key", () => {
    render(<AppShell />);
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    const chatContainer = screen.getByTestId("chat-container");
    window.localStorage.setItem("genus.chatDockOpen", "false");
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: "some.other.key", newValue: "x" }),
      );
    });
    expect(chatContainer.style.width).toBe("400px");
  });

  it("offers no collapse or reopen control on the chat home", () => {
    window.localStorage.setItem("genus.chatDockOpen", "false");
    render(<AppShell />);
    expect(screen.getByTestId("header-title")).toHaveTextContent("Chat");
    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-dock-close")).not.toBeInTheDocument();
    expect(screen.queryByTestId("chat-dock-open")).not.toBeInTheDocument();
  });

  it("shows review badge count from tasks", () => {
    render(<AppShell />);
    const badge = screen.getByTestId("badge-tasks");
    expect(badge.textContent).toBe("1"); // 1 REVIEW task
  });

  it("shows unhealthy badge count from agents", () => {
    render(<AppShell />);
    const badge = screen.getByTestId("badge-agents");
    expect(badge.textContent).toBe("1"); // 1 failed agent
  });

  it("calls useTasks with live=true", () => {
    render(<AppShell />);
    expect(mockUseTasks).toHaveBeenCalledWith({ live: true });
  });

  it("all three views are mounted simultaneously", () => {
    render(<AppShell />);
    expect(screen.getByTestId("dashboard-view")).toBeInTheDocument();
    expect(screen.getByTestId("tasks-view")).toBeInTheDocument();
    expect(screen.getByTestId("agents-view")).toBeInTheDocument();
  });
});
