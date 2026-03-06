import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

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
  default: (props: Record<string, unknown>) => <img {...props} />,
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

vi.mock("@/components/chat-panel", () => ({
  ChatPanel: () => <div data-testid="chat-panel">ChatPanel</div>,
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
    expect(screen.getByText("Robothor")).toBeInTheDocument();
  });

  it("header shows current view title", () => {
    render(<AppShell />);
    expect(screen.getByTestId("header-title")).toHaveTextContent("Dashboard");
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
    // 1 failed agent -> amber dot
    expect(dot.className).toContain("bg-amber");
  });

  it("renders chat panel", () => {
    render(<AppShell />);
    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
  });

  it("defaults to dashboard view", () => {
    render(<AppShell />);
    const dashView = screen.getByTestId("dashboard-view");
    expect(dashView.style.display).toBe("flex");
    const tasksView = screen.getByTestId("tasks-view");
    expect(tasksView.style.display).toBe("none");
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

  it("toggles chat panel", () => {
    render(<AppShell />);
    const chatContainer = screen.getByTestId("chat-container");
    // Initially open (400px)
    expect(chatContainer.style.width).toBe("400px");
    // Click chat toggle
    fireEvent.click(screen.getByTestId("nav-chat"));
    expect(chatContainer.style.width).toBe("0px");
    // Click again to reopen
    fireEvent.click(screen.getByTestId("nav-chat"));
    expect(chatContainer.style.width).toBe("400px");
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
