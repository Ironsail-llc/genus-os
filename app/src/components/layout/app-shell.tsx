"use client";

import { useState, useEffect } from "react";
import { Sidebar, type ViewId } from "./sidebar";
import { MobileTabBar, type MobileViewId } from "./mobile-tab-bar";
import { ChatPanel } from "@/components/chat-panel";
import { DashboardView } from "@/components/views/dashboard-view";
import { TasksView } from "@/components/views/tasks-view";
import { AgentsView } from "@/components/views/agents-view";
import { MarketplaceView } from "@/components/views/marketplace-view";
import { useTasks } from "@/hooks/use-tasks";
import { useAgents } from "@/hooks/use-agents";
import { useScreenSize } from "@/hooks/use-mobile";
import Image from "next/image";

const viewTitles: Record<ViewId, string> = {
  dashboard: "Dashboard",
  tasks: "Tasks",
  agents: "Agents",
  marketplace: "Marketplace",
};

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
  // Mobile defaults to chat — the primary interaction. Desktop defaults to dashboard.
  const screenSize = useScreenSize();
  const isMobile = screenSize === "mobile";
  const [activeView, setActiveView] = useState<MobileViewId>(() =>
    typeof window !== "undefined" && window.innerWidth < 768 ? "chat" : "dashboard"
  );
  const [chatOpen, setChatOpen] = useState(true);

  // Lift data fetching — single source for sidebar badges + views
  const { tasks, isLoading: tasksLoading, approveTask, rejectTask } = useTasks({ live: true });
  const { agents, summary: agentSummary, isLoading: agentsLoading } = useAgents();

  const reviewCount = tasks.filter((t) => t.status === "REVIEW").length;
  const unhealthyCount = agentSummary.degraded + agentSummary.failed;
  const allHealthy = agentSummary.failed === 0 && agentSummary.degraded === 0;

  // On mobile, "chat" is a view. On desktop, it's a side panel.
  const sidebarView = activeView === "chat" ? "dashboard" : activeView as ViewId;
  const showMainContent = isMobile ? activeView !== "chat" : true;
  const mobileInChat = isMobile && activeView === "chat";

  return (
    <div className="flex flex-col md:flex-row h-full w-full" data-testid="app-shell">
      {/* Desktop sidebar — hidden on mobile */}
      {!isMobile && (
        <Sidebar
          activeView={sidebarView}
          onViewChange={(v) => setActiveView(v)}
          chatOpen={chatOpen}
          onChatToggle={() => setChatOpen((prev) => !prev)}
          reviewCount={reviewCount}
          unhealthyCount={unhealthyCount}
        />
      )}

      {/* Main content area — hidden on mobile when chat is active */}
      {showMainContent && (
        <div className={`flex-1 min-w-0 min-h-0 flex flex-col relative ${isMobile ? "pb-14" : ""}`}>
          {/* Header bar */}
          <header
            className="h-10 shrink-0 flex items-center px-4 border-b border-border bg-background/80 backdrop-blur-sm"
            data-testid="header-bar"
          >
            <div className="flex items-center gap-2">
              <Image
                src="/robothor-bolt.svg"
                alt=""
                width={14}
                height={14}
                className="opacity-70"
              />
              <span className="text-sm font-semibold tracking-tight">Genus OS</span>
            </div>

            <span className="mx-auto text-xs font-medium text-muted-foreground" data-testid="header-title">
              {viewTitles[sidebarView]}
            </span>

            <div className="flex items-center gap-3">
              <div
                className={`w-1.5 h-1.5 rounded-full ${allHealthy ? "bg-emerald-400" : "bg-amber-400"}`}
                data-testid="system-status-dot"
              />
              <HeaderClock />
            </div>
          </header>

          {/* Main content area — all views stay mounted */}
          <div className="flex-1 min-h-0 relative">
            <DashboardView visible={sidebarView === "dashboard"} />
            <TasksView
              visible={sidebarView === "tasks"}
              tasks={tasks}
              isLoading={tasksLoading}
              onApprove={approveTask}
              onReject={rejectTask}
            />
            <AgentsView
              visible={sidebarView === "agents"}
              agents={agents}
              summary={agentSummary}
              isLoading={agentsLoading}
            />
            <MarketplaceView visible={sidebarView === "marketplace"} />
          </div>
        </div>
      )}

      {/* Chat panel — full screen on mobile (no duplicate header), side panel on desktop */}
      {mobileInChat ? (
        <div className="flex-1 min-h-0 pb-14" data-testid="chat-container">
          <div className="h-full">
            <ChatPanel mobile />
          </div>
        </div>
      ) : !isMobile ? (
        <div
          className="shrink-0 border-l border-border transition-[width] duration-200 overflow-hidden"
          style={{ width: chatOpen ? 400 : 0 }}
          data-testid="chat-container"
        >
          <div className="h-full w-[400px]">
            <ChatPanel />
          </div>
        </div>
      ) : null}

      {/* Mobile bottom tab bar — fixed to bottom, always visible */}
      {isMobile && (
        <MobileTabBar
          activeView={activeView}
          onViewChange={setActiveView}
          reviewCount={reviewCount}
          unhealthyCount={unhealthyCount}
        />
      )}
    </div>
  );
}
