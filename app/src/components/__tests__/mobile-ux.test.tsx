import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, within } from "@testing-library/react";

// ─── Shared mocks ────────────────────────────────────────────────
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

vi.mock("@/hooks/use-throttle", () => ({
  useThrottle: (value: string) => value,
}));

vi.mock("@/hooks/use-tasks", () => ({
  useTasks: () => ({
    tasks: [],
    isLoading: false,
    approveTask: vi.fn(),
    rejectTask: vi.fn(),
  }),
}));

vi.mock("@/hooks/use-agents", () => ({
  useAgents: () => ({
    agents: [],
    summary: { healthy: 3, degraded: 0, failed: 0, sleeping: 2, total: 5 },
    isLoading: false,
  }),
}));

vi.mock("@/hooks/use-dashboard-agent", () => ({
  useDashboardAgent: vi.fn(),
}));

vi.mock("@/lib/api/health", () => ({
  fetchHealth: vi.fn().mockResolvedValue({ status: "ok", services: [] }),
}));

vi.mock("@/lib/api/people", () => ({
  fetchPeople: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/lib/api/conversations", () => ({
  fetchConversations: vi.fn().mockResolvedValue([]),
}));

vi.mock("@/lib/api/memory", () => ({
  searchMemory: vi.fn().mockResolvedValue([]),
}));

vi.mock("cronstrue", () => ({
  default: { toString: (expr: string) => expr },
}));

// ─── Screen size helpers ─────────────────────────────────────────
function setViewport(width: number) {
  Object.defineProperty(window, "innerWidth", {
    writable: true,
    configurable: true,
    value: width,
  });
  window.dispatchEvent(new Event("resize"));
}

function mockMatchMedia(width: number) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => {
    let matches = false;
    if (query.includes("max-width: 767px")) {
      matches = width < 768;
    } else if (query.includes("min-width: 1024px")) {
      matches = width >= 1024;
    }
    return {
      matches,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    };
  });
}

// ─── Test data ───────────────────────────────────────────────────
const mockTasks = [
  { id: "1", title: "Fix login bug", status: "TODO" as const, priority: "high", body: "Users can't log in", tags: ["bug"] },
  { id: "2", title: "Deploy v2", status: "IN_PROGRESS" as const, priority: "normal", assignedToAgent: "email-classifier" },
  { id: "3", title: "Review PR #42", status: "REVIEW" as const, priority: "urgent", slaDeadlineAt: "2026-01-01T00:00:00Z" },
  { id: "4", title: "Old task done", status: "DONE" as const, priority: "low" },
];

const mockAgents = [
  { name: "email-classifier", schedule: "0 */6 * * *", status: "healthy" as const, lastRun: new Date().toISOString(), lastDuration: 13000 },
  { name: "morning-briefing", schedule: "30 6 * * *", status: "sleeping" as const, lastRun: new Date().toISOString(), lastDuration: 45000 },
  { name: "vision-monitor", schedule: "0 */6 * * *", status: "degraded" as const, lastRun: new Date().toISOString(), lastDuration: 25000, errorCount: 3 },
];

// ─── Tests ───────────────────────────────────────────────────────

describe("Mobile UX — AppShell", () => {
  beforeEach(() => {
    mockMatchMedia(375);
    setViewport(375);
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ messages: [], active: false }),
      text: () => Promise.resolve(JSON.stringify({ messages: [], active: false })),
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows mobile tab bar on small screens", async () => {
    const { AppShell } = await import("../layout/app-shell");
    render(<AppShell />);
    expect(screen.getByTestId("mobile-tab-bar")).toBeInTheDocument();
  });

  it("hides desktop sidebar on mobile", async () => {
    const { AppShell } = await import("../layout/app-shell");
    render(<AppShell />);
    expect(screen.queryByTestId("sidebar")).not.toBeInTheDocument();
  });

  it("defaults to chat view on mobile", async () => {
    const { AppShell } = await import("../layout/app-shell");
    render(<AppShell />);
    // Chat panel should be visible by default
    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
    // App header bar should NOT be visible (chat has its own header)
    expect(screen.queryByTestId("header-bar")).not.toBeInTheDocument();
  });

  it("shows 4 tabs: Chat, Dashboard, Tasks, Agents (chat first)", async () => {
    const { AppShell } = await import("../layout/app-shell");
    render(<AppShell />);
    const tabBar = screen.getByTestId("mobile-tab-bar");
    const tabs = tabBar.querySelectorAll("button");
    // Chat should be the first tab
    expect(tabs[0]).toHaveAttribute("data-testid", "mobile-tab-chat");
    expect(within(tabBar).getByTestId("mobile-tab-dashboard")).toBeInTheDocument();
    expect(within(tabBar).getByTestId("mobile-tab-tasks")).toBeInTheDocument();
    expect(within(tabBar).getByTestId("mobile-tab-agents")).toBeInTheDocument();
  });

  it("tab buttons meet 44px minimum touch target", async () => {
    const { AppShell } = await import("../layout/app-shell");
    render(<AppShell />);
    const tabButtons = screen.getByTestId("mobile-tab-bar").querySelectorAll("button");
    for (const btn of tabButtons) {
      expect(btn.className).toContain("min-w-[44px]");
      expect(btn.className).toContain("min-h-[44px]");
    }
  });

  it("tab bar is fixed to bottom and stays visible across all views", async () => {
    const { AppShell } = await import("../layout/app-shell");
    const { fireEvent } = await import("@testing-library/react");
    render(<AppShell />);

    // Tab bar should be fixed positioned
    const tabBar = screen.getByTestId("mobile-tab-bar");
    expect(tabBar.className).toContain("fixed");
    expect(tabBar.className).toContain("bottom-0");
    expect(tabBar.className).toContain("z-50");

    // Switch to dashboard — tab bar must persist
    fireEvent.click(screen.getByTestId("mobile-tab-dashboard"));
    expect(screen.getByTestId("mobile-tab-bar")).toBeInTheDocument();

    // Switch to tasks — tab bar must persist
    fireEvent.click(screen.getByTestId("mobile-tab-tasks"));
    expect(screen.getByTestId("mobile-tab-bar")).toBeInTheDocument();

    // Switch to agents — tab bar must persist
    fireEvent.click(screen.getByTestId("mobile-tab-agents"));
    expect(screen.getByTestId("mobile-tab-bar")).toBeInTheDocument();

    // Switch back to chat — tab bar must persist
    fireEvent.click(screen.getByTestId("mobile-tab-chat"));
    expect(screen.getByTestId("mobile-tab-bar")).toBeInTheDocument();
  });

  it("content areas have bottom padding to clear the fixed tab bar", async () => {
    const { AppShell } = await import("../layout/app-shell");
    const { fireEvent } = await import("@testing-library/react");
    render(<AppShell />);

    // In chat view, the chat container should have pb-14
    const chatContainer = screen.getByTestId("chat-container");
    expect(chatContainer.className).toContain("pb-14");

    // Switch to dashboard — main content should have pb-14
    fireEvent.click(screen.getByTestId("mobile-tab-dashboard"));
    const mainContent = screen.getByTestId("header-bar").parentElement!;
    expect(mainContent.className).toContain("pb-14");
  });
});

describe("Mobile UX — TaskBoard", () => {
  it("renders as stacked list on mobile viewports, not 4-column grid", async () => {
    const { TaskBoard } = await import("../business/task-board");
    render(
      <TaskBoard tasks={mockTasks} onApprove={vi.fn()} onReject={vi.fn()} />
    );
    const board = screen.getByTestId("task-board");
    expect(board.className).toMatch(/grid-cols-1/);
  });

  it("task cards are readable width", async () => {
    const { TaskBoard } = await import("../business/task-board");
    render(
      <TaskBoard tasks={mockTasks} onApprove={vi.fn()} onReject={vi.fn()} />
    );
    const cards = screen.getAllByTestId("task-card");
    expect(cards.length).toBeGreaterThan(0);
  });

  it("review action buttons are tap-friendly on mobile", async () => {
    const { TaskBoard } = await import("../business/task-board");
    render(
      <TaskBoard tasks={mockTasks} onApprove={vi.fn()} onReject={vi.fn()} />
    );
    const reviewActions = screen.getByTestId("review-actions");
    const approveBtn = within(reviewActions).getByTestId("approve-button");
    const rejectBtn = within(reviewActions).getByTestId("reject-button");
    expect(approveBtn.className).toMatch(/h-8/);
    expect(rejectBtn.className).toMatch(/h-8/);
  });
});

describe("Mobile UX — ChatPanel (mobile mode)", () => {
  beforeEach(() => {
    mockMatchMedia(375);
    setViewport(375);
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ messages: [], active: false }),
      text: () => Promise.resolve(JSON.stringify({ messages: [], active: false })),
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows labeled mode toggle chips on mobile", async () => {
    const { ChatPanel } = await import("../chat-panel");
    render(<ChatPanel mobile />);
    const toggles = screen.getByTestId("mobile-mode-toggles");
    expect(toggles).toBeInTheDocument();
    // Should have labeled buttons, not just icons
    const planToggle = within(toggles).getByTestId("plan-toggle");
    expect(planToggle.textContent).toContain("Plan");
    const deepToggle = within(toggles).getByTestId("deep-toggle");
    expect(deepToggle.textContent).toContain("Deep");
  });

  it("does NOT show desktop icon-only toggles in mobile mode", async () => {
    const { ChatPanel } = await import("../chat-panel");
    render(<ChatPanel mobile />);
    // The TooltipProvider-wrapped buttons should not exist
    const toggleArea = screen.getByTestId("mobile-mode-toggles");
    expect(toggleArea).toBeInTheDocument();
    // Only 2 plan/deep toggles total (mobile chips), not 4 (mobile + desktop)
    const allPlanToggles = screen.getAllByTestId("plan-toggle");
    expect(allPlanToggles).toHaveLength(1);
  });

  it("chat input has min-height for touch accessibility", async () => {
    const { ChatPanel } = await import("../chat-panel");
    render(<ChatPanel mobile />);
    const input = screen.getByTestId("chat-input");
    expect(input.className).toContain("min-h-[44px]");
  });

  it("send button is present", async () => {
    const { ChatPanel } = await import("../chat-panel");
    render(<ChatPanel mobile />);
    expect(screen.getByTestId("send-button")).toBeInTheDocument();
  });

  it("suggested prompts have touch-friendly sizing", async () => {
    const { ChatPanel } = await import("../chat-panel");
    render(<ChatPanel mobile />);
    const prompts = screen.getAllByTestId("suggested-prompt");
    for (const prompt of prompts) {
      expect(prompt.className).toContain("min-h-[44px]");
    }
  });

  it("header shows Online status on mobile", async () => {
    const { ChatPanel } = await import("../chat-panel");
    render(<ChatPanel mobile />);
    expect(screen.getByText("Online")).toBeInTheDocument();
  });

  it("messages use max-width for readability", async () => {
    const { ChatPanel } = await import("../chat-panel");

    global.fetch = vi.fn().mockImplementation((url: string) => {
      if (typeof url === "string" && url.includes("/history")) {
        const data = {
          messages: [
            { role: "assistant", content: "Hello! How can I help?" },
          ],
        };
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(data),
          text: () => Promise.resolve(JSON.stringify(data)),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ active: false }),
        text: () => Promise.resolve(JSON.stringify({ active: false })),
      });
    }) as unknown as typeof fetch;

    render(<ChatPanel mobile />);
    const msg = await screen.findByTestId("message-assistant");
    expect(msg.querySelector("div")?.className).toContain("max-w-");
  });
});

describe("Mobile UX — ChatPanel (desktop mode)", () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ messages: [], active: false }),
      text: () => Promise.resolve(JSON.stringify({ messages: [], active: false })),
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows icon-only toggles with tooltips on desktop", async () => {
    const { ChatPanel } = await import("../chat-panel");
    render(<ChatPanel />);
    // Should NOT have mobile mode toggles
    expect(screen.queryByTestId("mobile-mode-toggles")).not.toBeInTheDocument();
    // Should have desktop toggles
    expect(screen.getByTestId("plan-toggle")).toBeInTheDocument();
    expect(screen.getByTestId("deep-toggle")).toBeInTheDocument();
  });
});

describe("Mobile UX — AgentStatus", () => {
  it("agent grid uses single column on mobile", async () => {
    const { AgentStatus } = await import("../business/agent-status");
    render(
      <AgentStatus
        agents={mockAgents}
        summary={{ healthy: 1, degraded: 1, failed: 0, sleeping: 1, total: 3 }}
      />
    );
    const grid = screen.getByTestId("agent-status").querySelector(".grid");
    expect(grid?.className).toMatch(/grid-cols-1/);
  });

  it("summary badges wrap on narrow screens", async () => {
    const { AgentStatus } = await import("../business/agent-status");
    render(
      <AgentStatus
        agents={mockAgents}
        summary={{ healthy: 1, degraded: 1, failed: 0, sleeping: 1, total: 3 }}
      />
    );
    const summary = screen.getByTestId("agent-summary");
    expect(summary.className).toContain("flex-wrap");
  });
});

describe("Mobile UX — DefaultDashboard", () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: "ok", services: [] }),
      text: () => Promise.resolve(JSON.stringify({ status: "ok", services: [] })),
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("metric summary uses responsive columns", async () => {
    const { DefaultDashboard } = await import("../business/default-dashboard");
    render(<DefaultDashboard />);
    const metricSummary = screen.getByTestId("metric-summary");
    expect(metricSummary.className).toMatch(/grid-cols-2/);
  });

  it("quick actions use 2 columns on mobile", async () => {
    const { DefaultDashboard } = await import("../business/default-dashboard");
    render(<DefaultDashboard />);
    const quickActions = screen.getAllByTestId("quick-action");
    expect(quickActions.length).toBe(4);
    const grid = quickActions[0].parentElement;
    expect(grid?.className).toContain("grid-cols-2");
  });

  it("quick action buttons have adequate touch targets", async () => {
    const { DefaultDashboard } = await import("../business/default-dashboard");
    render(<DefaultDashboard />);
    const quickActions = screen.getAllByTestId("quick-action");
    for (const action of quickActions) {
      expect(action.className).toContain("p-4");
    }
  });
});

describe("Mobile UX — MetricGrid", () => {
  it("uses 2 columns on mobile", async () => {
    const { MetricGrid } = await import("../business/metric-grid");
    render(
      <MetricGrid
        metrics={[
          { title: "Tasks", value: 5 },
          { title: "Agents", value: 3 },
          { title: "Health", value: "OK" },
        ]}
      />
    );
    const grid = screen.getByTestId("metric-grid");
    expect(grid.className).toContain("grid-cols-2");
  });
});

describe("Mobile UX — MobileTabBar", () => {
  it("has safe-area-bottom class for notched phones", async () => {
    const { MobileTabBar } = await import("../layout/mobile-tab-bar");
    render(
      <MobileTabBar
        activeView="chat"
        onViewChange={vi.fn()}
        reviewCount={0}
        unhealthyCount={0}
      />
    );
    const tabBar = screen.getByTestId("mobile-tab-bar");
    expect(tabBar.className).toContain("safe-area-bottom");
  });

  it("chat tab is first in the tab bar", async () => {
    const { MobileTabBar } = await import("../layout/mobile-tab-bar");
    render(
      <MobileTabBar
        activeView="chat"
        onViewChange={vi.fn()}
        reviewCount={0}
        unhealthyCount={0}
      />
    );
    const tabs = screen.getByTestId("mobile-tab-bar").querySelectorAll("button");
    expect(tabs[0]).toHaveAttribute("data-testid", "mobile-tab-chat");
  });

  it("shows badge counts on tabs", async () => {
    const { MobileTabBar } = await import("../layout/mobile-tab-bar");
    render(
      <MobileTabBar
        activeView="chat"
        onViewChange={vi.fn()}
        reviewCount={5}
        unhealthyCount={2}
      />
    );
    const tasksTab = screen.getByTestId("mobile-tab-tasks");
    expect(tasksTab.textContent).toContain("5");
    const agentsTab = screen.getByTestId("mobile-tab-agents");
    expect(agentsTab.textContent).toContain("2");
  });

  it("highlights active tab with primary color", async () => {
    const { MobileTabBar } = await import("../layout/mobile-tab-bar");
    render(
      <MobileTabBar
        activeView="chat"
        onViewChange={vi.fn()}
        reviewCount={0}
        unhealthyCount={0}
      />
    );
    const chatTab = screen.getByTestId("mobile-tab-chat");
    expect(chatTab.className).toContain("text-primary");
    const dashTab = screen.getByTestId("mobile-tab-dashboard");
    expect(dashTab.className).toContain("text-muted-foreground");
  });

  it("chat tab has visual accent (background highlight) when active", async () => {
    const { MobileTabBar } = await import("../layout/mobile-tab-bar");
    render(
      <MobileTabBar
        activeView="chat"
        onViewChange={vi.fn()}
        reviewCount={0}
        unhealthyCount={0}
      />
    );
    const chatTab = screen.getByTestId("mobile-tab-chat");
    // Chat tab wraps icon in a highlighted container when active
    const highlightDiv = chatTab.querySelector(".rounded-full");
    expect(highlightDiv?.className).toContain("bg-primary/15");
  });
});

describe("Mobile UX — ServiceHealth", () => {
  it("service cards use responsive grid", async () => {
    const { ServiceHealth } = await import("../business/service-health");
    render(
      <ServiceHealth
        services={[
          { name: "Engine", url: "http://localhost:18800/health", status: "healthy", responseTime: 45 },
          { name: "Bridge", url: "http://localhost:9100/health", status: "healthy", responseTime: 120 },
          { name: "Vision", url: "http://localhost:8600/health", status: "unhealthy" },
        ]}
        overallStatus="ok"
      />
    );
    const grid = screen.getByTestId("service-health").querySelector(".grid");
    expect(grid?.className).toMatch(/grid-cols-2/);
  });
});
