import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { useEffect, useState } from "react";

/**
 * The shell downloads and mounts a view only when it is first opened, then
 * keeps it mounted (hidden) so its state survives switching away.
 *
 * Every view module is replaced by a stand-in whose factory records that the
 * module was imported. A factory runs only when something imports the module,
 * so the record is exactly the set of view modules the shell asked for.
 */
const lazy = vi.hoisted(() => ({
  loaded: [] as string[],
  mounts: {} as Record<string, number>,
}));

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ viewStack: [], pendingAgentData: null }),
}));
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
    summary: { healthy: 1, degraded: 0, failed: 0, sleeping: 0, total: 1 },
    isLoading: false,
  }),
}));
vi.mock("@/hooks/use-inbox", () => ({
  useInbox: () => ({
    items: [],
    count: 0,
    isLoading: false,
    error: null,
    unrenderable: 0,
    refusedAsNonOperator: false,
    refresh: vi.fn(),
    answer: vi.fn(),
  }),
}));
vi.mock("@/components/chat-panel", () => ({
  ChatPanel: () => <div data-testid="chat-panel" />,
}));
vi.mock("@/components/canvas/live-canvas", () => ({
  LiveCanvas: () => <div data-testid="live-canvas" />,
}));
vi.mock("@/components/mfa-setup-banner", () => ({ MfaSetupBanner: () => null }));
const reload = vi.hoisted(() => ({ page: vi.fn() }));
vi.mock("@/lib/reload-page", () => ({ reloadPage: () => reload.page() }));
vi.mock("next-auth/react", () => ({
  useSession: () => ({ data: { role: "owner" }, status: "authenticated", update: vi.fn() }),
  SessionProvider: ({ children }: { children: React.ReactNode }) => children,
  signOut: vi.fn(),
  signIn: vi.fn(),
}));

/** A stand-in view: counts its mounts and holds a bit of state of its own. */
function stub(name: string) {
  lazy.loaded.push(name);
  return function StubView({ visible = true }: { visible?: boolean }) {
    const [clicks, setClicks] = useState(0);
    useEffect(() => {
      lazy.mounts[name] = (lazy.mounts[name] ?? 0) + 1;
    }, []);
    return (
      <div data-testid={`stub-${name}`} hidden={!visible} data-clicks={clicks}>
        <button data-testid={`stub-${name}-click`} onClick={() => setClicks((c) => c + 1)} />
      </div>
    );
  };
}

/**
 * Registered with `doMock` in `beforeEach`, after `resetModules`, so every test
 * starts with fresh factories: a factory registered once with `vi.mock` would
 * stay cached across tests and record its import only in the first.
 */
const VIEW_MODULES: Array<[path: string, exportName: string, name: string]> = [
  ["@/components/views/dashboard-view", "DashboardView", "dashboard"],
  ["@/components/views/sales-view", "SalesView", "sales"],
  ["@/components/views/inbox-view", "InboxView", "inbox"],
  ["@/components/views/goals-view", "GoalsView", "goals"],
  ["@/components/views/tasks-view", "TasksView", "tasks"],
  ["@/components/views/agents-view", "AgentsView", "agents"],
  ["@/components/views/marketplace-view", "MarketplaceView", "marketplace"],
  ["@/components/views/fleet-view", "FleetView", "fleet"],
  ["@/components/views/runs-view", "RunsView", "runs"],
  ["@/components/views/automations-view", "AutomationsView", "workflows"],
  ["@/components/views/health-view", "HealthView", "health"],
  ["@/components/views/memory-view", "MemoryView", "memory"],
  ["@/components/views/audit-view", "AuditView", "audit"],
  ["@/components/views/logs-view", "LogsView", "logs"],
  ["@/components/views/coming-soon-view", "ComingSoonView", "coming-soon"],
  ["@/components/views/settings-view", "SettingsView", "settings"],
];

async function renderShellAt(search: string) {
  window.history.replaceState(null, "", `/${search}`);
  const { AppShell } = await import("../app-shell");
  return render(<AppShell />);
}

describe("AppShell — views load on first visit", () => {
  beforeEach(() => {
    vi.resetModules();
    lazy.loaded = [];
    lazy.mounts = {};
    reload.page.mockClear();
    for (const [path, exportName, name] of VIEW_MODULES) {
      vi.doMock(path, () => ({ [exportName]: stub(name) }));
    }
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("imports no view module while the chat view is showing", async () => {
    await renderShellAt("?v=chat");
    // Give any eager import a chance to settle before counting.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.getByTestId("header-title").textContent).toBe("Chat");
    expect(lazy.loaded).toEqual([]);
  });

  it("imports only the active view's module on a deep link", async () => {
    await renderShellAt("?v=tasks");
    expect(await screen.findByTestId("stub-tasks")).toBeVisible();
    expect(lazy.loaded).toEqual(["tasks"]);
  });

  it("shows a skeleton while the view's chunk is loading", async () => {
    await renderShellAt("?v=fleet");
    expect(screen.getByTestId("view-loading")).toBeInTheDocument();
    expect(await screen.findByTestId("stub-fleet")).toBeVisible();
    expect(screen.queryByTestId("view-loading")).toBeNull();
  });

  it("keeps a visited view mounted, with its state, after switching away", async () => {
    await renderShellAt("?v=goals");
    fireEvent.click(await screen.findByTestId("stub-goals-click"));
    expect(screen.getByTestId("stub-goals")).toHaveAttribute("data-clicks", "1");

    fireEvent.click(screen.getByTestId("nav-tasks"));
    expect(await screen.findByTestId("stub-tasks")).toBeVisible();
    // Still in the document, only hidden, and never remounted.
    expect(screen.getByTestId("stub-goals")).not.toBeVisible();

    fireEvent.click(screen.getByTestId("nav-goals"));
    expect(screen.getByTestId("stub-goals")).toBeVisible();
    expect(screen.getByTestId("stub-goals")).toHaveAttribute("data-clicks", "1");
    expect(lazy.mounts.goals).toBe(1);
    expect(lazy.loaded).toEqual(["goals", "tasks"]);
  });

  it("does not mount a view that was never opened", async () => {
    await renderShellAt("?v=runs");
    await screen.findByTestId("stub-runs");
    expect(screen.queryByTestId("stub-settings")).toBeNull();
    expect(screen.queryByTestId("stub-memory")).toBeNull();
    expect(Object.keys(lazy.mounts)).toEqual(["runs"]);
  });

  /**
   * A tab opened before a deploy asks for chunk hashes the new build no longer
   * serves. That failure must cost the one view, not the Helm, and say what
   * fixes it: a reload.
   */
  it("keeps the Helm up when a view's chunk fails to load, and offers a reload", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.doMock("@/components/views/goals-view", () => {
      // The export throws on access, as the dynamic import's `.then` reads it:
      // the shell sees the loader reject with the chunk error itself. (An error
      // thrown by the factory reaches it wrapped in vitest's own message.)
      const error = new Error("Failed to load chunk /_next/static/chunks/goals.js");
      error.name = "ChunkLoadError";
      return {
        get GoalsView() {
          throw error;
        },
      };
    });
    await renderShellAt("?v=goals");

    const panel = await screen.findByTestId("view-load-failed");
    expect(panel.textContent).toMatch(/updated/i);
    expect(screen.getByTestId("sidebar")).toBeInTheDocument();
    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("view-load-failed-reload"));
    expect(reload.page).toHaveBeenCalledTimes(1);

    // Another screen still works, and the failed one's panel leaves with it.
    fireEvent.click(screen.getByTestId("nav-tasks"));
    expect(await screen.findByTestId("stub-tasks")).toBeVisible();
    expect(screen.queryByTestId("view-load-failed")).toBeNull();
  });
});
