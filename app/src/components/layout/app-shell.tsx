"use client";

import { useState, useEffect } from "react";
import { useSession } from "next-auth/react";
import { Sidebar } from "./sidebar";
import { MobileTabBar } from "./mobile-tab-bar";
import { isComingSoonView, viewGroupLabel, viewTitles } from "./nav-config";
import { ChatPanel } from "@/components/chat-panel";
import { DashboardView } from "@/components/views/dashboard-view";
import { TasksView } from "@/components/views/tasks-view";
import { AgentsView } from "@/components/views/agents-view";
import { MarketplaceView } from "@/components/views/marketplace-view";
import { FleetView } from "@/components/views/fleet-view";
import { RunsView } from "@/components/views/runs-view";
import { WorkflowsView } from "@/components/views/workflows-view";
import { HealthView } from "@/components/views/health-view";
import { CanvasView } from "@/components/views/canvas-view";
import { ComingSoonView } from "@/components/views/coming-soon-view";
import { SettingsView } from "@/components/views/settings-view";
import { MfaSetupBanner } from "@/components/mfa-setup-banner";
import { ThemeToggle } from "@/components/business/theme-toggle";
import { CommandPalette } from "@/components/business/command-palette";
import { useTasks } from "@/hooks/use-tasks";
import { useAgents } from "@/hooks/use-agents";
import { useScreenSize } from "@/hooks/use-mobile";
import { useViewRoute } from "@/hooks/use-view-route";
import { PanelRight, Zap } from "lucide-react";

function HeaderClock() {
  const [time, setTime] = useState("");

  useEffect(() => {
    const update = () => {
      setTime(
        new Date().toLocaleTimeString("en-US", {
          hour: "numeric",
          minute: "2-digit",
          timeZone: "America/New_York",
        })
      );
    };
    update();
    const interval = setInterval(update, 60_000);
    return () => clearInterval(interval);
  }, []);

  return <span className="text-xs text-muted-foreground tabular-nums">{time} ET</span>;
}

export function AppShell() {
  const screenSize = useScreenSize();
  const isMobile = screenSize === "mobile";

  // Chat-first on both desktop and mobile; the view round-trips through ?v=.
  const { view, settingsPage, navigate } = useViewRoute();

  // The role the auth layer already exposes. Used for nav gating only — server
  // -side authorization of these screens is NOT this component's job and is
  // enforced independently by the bridge on every request.
  //
  // `status` matters: the first client render of a next-auth session is
  // "loading" with no data, and an owner must not be told a screen is not
  // theirs while the role is merely unknown.
  const { data: session, status } = useSession();
  const roleLoading = status === "loading";
  const role = session?.role;

  const [chatOpen, setChatOpen] = useState(true);
  const [canvasRailOpen, setCanvasRailOpen] = useState(false);

  // Lift data fetching — single source for sidebar badges + views
  const { tasks, isLoading: tasksLoading, approveTask, rejectTask, answerQuestion } =
    useTasks({ live: true });
  const { agents, summary: agentSummary, isLoading: agentsLoading } = useAgents();

  const reviewCount = tasks.filter((t) => t.status === "REVIEW").length;
  const unhealthyCount = agentSummary.degraded + agentSummary.failed;
  const allHealthy = agentSummary.failed === 0 && agentSummary.degraded === 0;

  const isChatView = view === "chat";
  // On mobile chat is full-screen with its own header; on desktop it is the
  // main column, optionally flanked by the canvas rail.
  const mobileInChat = isMobile && isChatView;
  const showCanvasRail = !isMobile && isChatView && canvasRailOpen;

  return (
    <div className="flex h-full w-full flex-col">
      {/* Above the shell, not inside a view: an owner who owes a second factor
          must see it whatever they navigate to, and it has no dismiss. */}
      <MfaSetupBanner />
      <div
        className="flex flex-col md:flex-row flex-1 min-h-0 w-full"
        data-testid="app-shell"
      >
      <CommandPalette onNavigate={navigate} role={role} />
      {/* Desktop sidebar — hidden on mobile */}
      {!isMobile && (
        <Sidebar
          activeView={view}
          activeSettingsPage={settingsPage}
          onNavigate={navigate}
          chatOpen={chatOpen}
          onChatToggle={() => setChatOpen((prev) => !prev)}
          reviewCount={reviewCount}
          unhealthyCount={unhealthyCount}
          role={role}
        />
      )}

      {/* Main content area — replaced by the chat panel on mobile */}
      {!mobileInChat && (
        <div className={`flex-1 min-w-0 min-h-0 flex flex-col relative ${isMobile ? "pb-14" : ""}`}>
          {/* Header bar */}
          <header
            className="h-12 shrink-0 flex items-center gap-3 px-4 border-b border-border bg-background/80 backdrop-blur-sm"
            data-testid="header-bar"
          >
            {/* Brand appears here only on mobile — desktop carries it in the sidebar */}
            {isMobile && (
              <span
                aria-hidden
                className="flex size-5 items-center justify-center rounded-md bg-gradient-to-br from-primary to-brand-2 text-white"
              >
                <Zap className="size-3" fill="currentColor" strokeWidth={0} />
              </span>
            )}

            <div className="flex items-baseline gap-1.5 min-w-0">
              {!isMobile && (
                <span className="text-xs text-muted-foreground/70">{viewGroupLabel(view)} /</span>
              )}
              <span className="text-sm font-semibold tracking-tight truncate" data-testid="header-title">
                {viewTitles[view]}
              </span>
            </div>

            <div className="ml-auto flex items-center gap-3">
              {!isMobile && isChatView && (
                <button
                  onClick={() => setCanvasRailOpen((prev) => !prev)}
                  aria-pressed={canvasRailOpen}
                  data-testid="canvas-rail-toggle"
                  className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition-colors ${
                    canvasRailOpen
                      ? "border-primary/25 bg-primary/10 text-foreground"
                      : "border-border text-muted-foreground hover:text-foreground"
                  }`}
                >
                  <PanelRight className="size-3.5" />
                  Canvas
                </button>
              )}
              {!isMobile && (
                <kbd className="rounded-md border border-border bg-card px-1.5 py-0.5 font-mono text-[10.5px] text-muted-foreground">
                  ⌘K
                </kbd>
              )}
              <span
                className={`inline-flex items-center gap-1.5 text-xs font-medium ${allHealthy ? "text-success" : "text-warning"}`}
              >
                <span
                  className={`w-1.5 h-1.5 rounded-full ${allHealthy ? "bg-success" : "bg-warning"}`}
                  data-testid="system-status-dot"
                />
                {!isMobile && (allHealthy ? "Nominal" : `${unhealthyCount} unhealthy`)}
              </span>
              <HeaderClock />
              <ThemeToggle />
            </div>
          </header>

          <div className="flex-1 min-h-0 flex">
            {/* Views stay mounted; the chat view hands the width to the panel. */}
            <div
              className={`relative flex-1 min-w-0 min-h-0 ${isChatView && !isMobile ? "hidden" : ""}`}
              data-testid="views-container"
            >
              <DashboardView visible={view === "dashboard"} />
              <TasksView
                visible={view === "tasks"}
                tasks={tasks}
                isLoading={tasksLoading}
                onApprove={approveTask}
                onReject={rejectTask}
                onAnswer={answerQuestion}
              />
              <AgentsView
                visible={view === "agents"}
                agents={agents}
                summary={agentSummary}
                isLoading={agentsLoading}
                role={role}
                roleLoading={roleLoading}
              />
              <MarketplaceView visible={view === "marketplace"} />
              <FleetView visible={view === "fleet"} />
              <RunsView visible={view === "runs"} />
              <WorkflowsView visible={view === "workflows"} />
              <HealthView visible={view === "health"} />
              <SettingsView
                visible={view === "settings"}
                page={settingsPage}
                onPageChange={(page) => navigate("settings", page)}
                role={role}
                roleLoading={roleLoading}
              />
              {isComingSoonView(view) && <ComingSoonView view={view} />}
            </div>

            {/* Chat — docked rail beside other views, the main column on chat. */}
            {!isMobile && (
              <div
                className={
                  isChatView
                    ? "flex-1 min-w-0 min-h-0"
                    : "shrink-0 border-l border-border transition-[width] duration-200 overflow-hidden"
                }
                style={isChatView ? undefined : { width: chatOpen ? 400 : 0 }}
                data-testid="chat-container"
              >
                <div className={isChatView ? "h-full w-full" : "h-full w-[400px]"}>
                  <ChatPanel />
                </div>
              </div>
            )}

            {/* The LLM canvas is an optional rail on the chat view. */}
            {showCanvasRail && (
              <aside
                className="w-[380px] shrink-0 min-h-0 border-l border-border"
                data-testid="canvas-rail"
                aria-label="Canvas"
              >
                <CanvasView visible />
              </aside>
            )}
          </div>
        </div>
      )}

      {/* Mobile chat — full screen, no duplicate header */}
      {mobileInChat && (
        <div className="flex-1 min-h-0 pb-14" data-testid="chat-container">
          <div className="h-full">
            <ChatPanel mobile />
          </div>
        </div>
      )}

      {/* Mobile bottom tab bar — fixed to bottom, always visible */}
      {isMobile && (
        <MobileTabBar
          activeView={view}
          activeSettingsPage={settingsPage}
          onNavigate={navigate}
          reviewCount={reviewCount}
          unhealthyCount={unhealthyCount}
          role={role}
        />
      )}
      </div>
    </div>
  );
}
