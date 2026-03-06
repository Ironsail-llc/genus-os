import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

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

// Mock hooks
vi.mock("@/hooks/use-tasks", () => ({
  useTasks: () => ({
    tasks: [],
    isLoading: false,
    refetch: vi.fn(),
    updateTaskStatus: vi.fn(),
    approveTask: vi.fn(),
    rejectTask: vi.fn(),
  }),
}));

vi.mock("@/hooks/use-agents", () => ({
  useAgents: () => ({
    agents: [],
    summary: { healthy: 0, degraded: 0, failed: 0, sleeping: 0, total: 0 },
    isLoading: false,
    refetch: vi.fn(),
  }),
}));

vi.mock("@/components/canvas/live-canvas", () => ({
  LiveCanvas: () => <div data-testid="live-canvas">LiveCanvas</div>,
}));

vi.mock("@/components/chat-panel", () => ({
  ChatPanel: () => <div data-testid="chat-panel">ChatPanel</div>,
}));

vi.mock("@/components/business/task-board", () => ({
  TaskBoard: () => <div data-testid="task-board">TaskBoard</div>,
}));

vi.mock("@/components/business/agent-status", () => ({
  AgentStatus: () => <div data-testid="agent-status">AgentStatus</div>,
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

import { AppShell } from "@/components/layout/app-shell";

describe("Layout (AppShell)", () => {
  it("renders the app shell container", () => {
    render(<AppShell />);
    expect(screen.getByTestId("app-shell")).toBeInTheDocument();
  });

  it("contains sidebar, dashboard view, and chat panel", () => {
    render(<AppShell />);
    expect(screen.getByTestId("sidebar")).toBeInTheDocument();
    expect(screen.getByTestId("dashboard-view")).toBeInTheDocument();
    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
  });

  it("renders all three views (display:none pattern)", () => {
    render(<AppShell />);
    expect(screen.getByTestId("dashboard-view")).toBeInTheDocument();
    expect(screen.getByTestId("tasks-view")).toBeInTheDocument();
    expect(screen.getByTestId("agents-view")).toBeInTheDocument();
  });
});
