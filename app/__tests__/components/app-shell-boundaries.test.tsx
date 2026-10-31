import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// Switches the stand-ins read at render time.
let chatThrows = false;
let tasksThrow = false;

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
  ChatPanel: (props: Parameters<typeof StubChat>[0]) => {
    if (chatThrows) throw new Error("chat render failed");
    return StubChat(props);
  },
}));

function StubChat({
  variant,
  ownerName,
  docked,
  onCollapse,
  onViewOpened,
}: {
  variant?: string;
  ownerName?: string;
  docked?: boolean;
  onCollapse?: () => void;
  onViewOpened?: () => void;
}) {
  return (
    <div
      data-testid="chat-panel"
      data-variant={variant ?? ""}
      data-owner={ownerName ?? ""}
      data-view-opened-handler={onViewOpened ? "yes" : "no"}
    >
      ChatPanel
      {docked && (
        <button data-testid="chat-dock-close" aria-label="Collapse chat panel" onClick={onCollapse} />
      )}
      {onViewOpened && (
        <button data-testid="panel-view-opened" onClick={onViewOpened}>
          opened
        </button>
      )}
    </div>
  );
}

// The session the shell reads the owner's name from; no session by default.
const mockUseSession = vi.fn();
vi.mock("next-auth/react", () => ({
  useSession: () => mockUseSession(),
  SessionProvider: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock("@/components/business/task-board", () => ({
  TaskBoard: ({ tasks }: { tasks: unknown[] }) => {
    if (tasksThrow) throw new Error("task board render failed");
    return <div data-testid="task-board">{tasks.length} tasks</div>;
  },
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

const mockScreenSize = vi.fn(() => "desktop");
vi.mock("@/hooks/use-mobile", () => ({
  useScreenSize: () => mockScreenSize(),
  useIsMobile: () => mockScreenSize() === "mobile",
}));

import { AppShell } from "@/components/layout/app-shell";

/**
 * One surface failing to render must not take the shell down: the chat panel
 * and each view sit behind their own boundary, mounted by the shell.
 */
describe("AppShell surface boundaries", () => {
  beforeEach(() => {
    chatThrows = false;
    tasksThrow = false;
    window.history.replaceState(null, "", "/");
    window.localStorage.clear();
    vi.spyOn(console, "error").mockImplementation(() => {});
    mockUseSession.mockReturnValue({ data: null, status: "unauthenticated", update: vi.fn() });
    mockScreenSize.mockReturnValue("desktop");
    mockUseTasks.mockReturnValue({
      tasks: [{ id: "1", title: "Test Task", status: "TODO" }],
      isLoading: false,
      refetch: vi.fn(),
      updateTaskStatus: vi.fn(),
      approveTask: vi.fn(),
      rejectTask: vi.fn(),
    });
    mockUseAgents.mockReturnValue({
      agents: [],
      summary: { healthy: 0, degraded: 0, failed: 0, sleeping: 0, total: 0 },
      isLoading: false,
      refetch: vi.fn(),
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps the sidebar and views when the chat panel throws", () => {
    chatThrows = true;
    render(<AppShell />);
    expect(screen.getByTestId("sidebar")).toBeInTheDocument();
    const notice = screen.getByTestId("surface-error-chat");
    expect(notice).toHaveTextContent(/Chat/);
    fireEvent.click(screen.getByTestId("nav-tasks"));
    expect(screen.getByTestId("task-board")).toBeInTheDocument();
  });

  it("keeps the chat panel and other views when one view throws", () => {
    tasksThrow = true;
    render(<AppShell />);
    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
    // The failed view is hidden while another view is active.
    expect(screen.queryByTestId("surface-error-tasks")).toBeNull();
    fireEvent.click(screen.getByTestId("nav-tasks"));
    expect(screen.getByTestId("surface-error-tasks")).toHaveTextContent(/Tasks/);
    fireEvent.click(screen.getByTestId("nav-dashboard"));
    expect(screen.getByTestId("dashboard-view").style.display).toBe("flex");
    expect(screen.queryByTestId("surface-error-tasks")).toBeNull();
  });

  it("re-mounts the chat panel on Retry", () => {
    chatThrows = true;
    render(<AppShell />);
    chatThrows = false;
    fireEvent.click(
      screen.getByTestId("surface-error-chat").querySelector("button") as HTMLButtonElement,
    );
    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
  });
});
