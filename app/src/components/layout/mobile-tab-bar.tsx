"use client";

import { LayoutDashboard, ListTodo, Bot, MessageSquare } from "lucide-react";

export type MobileViewId = "dashboard" | "tasks" | "agents" | "chat";

interface TabItem {
  id: MobileViewId;
  icon: React.ReactNode;
  label: string;
}

const tabs: TabItem[] = [
  { id: "chat", icon: <MessageSquare className="w-5 h-5" />, label: "Chat" },
  { id: "dashboard", icon: <LayoutDashboard className="w-5 h-5" />, label: "Dashboard" },
  { id: "tasks", icon: <ListTodo className="w-5 h-5" />, label: "Tasks" },
  { id: "agents", icon: <Bot className="w-5 h-5" />, label: "Agents" },
];

interface MobileTabBarProps {
  activeView: MobileViewId;
  onViewChange: (view: MobileViewId) => void;
  reviewCount: number;
  unhealthyCount: number;
}

export function MobileTabBar({
  activeView,
  onViewChange,
  reviewCount,
  unhealthyCount,
}: MobileTabBarProps) {
  const badgeCounts: Record<string, number> = {
    tasks: reviewCount,
    agents: unhealthyCount,
  };

  return (
    <nav
      className="fixed bottom-0 left-0 right-0 z-50 flex items-center justify-around h-14 border-t border-border bg-background/95 backdrop-blur-md safe-area-bottom"
      data-testid="mobile-tab-bar"
    >
      {tabs.map((tab) => {
        const isActive = activeView === tab.id;
        const isChat = tab.id === "chat";
        const badge = badgeCounts[tab.id] || 0;
        return (
          <button
            key={tab.id}
            onClick={() => onViewChange(tab.id)}
            className={`relative flex flex-col items-center justify-center min-w-[44px] min-h-[44px] gap-0.5 transition-colors ${
              isActive
                ? isChat
                  ? "text-primary"
                  : "text-primary"
                : isChat
                  ? "text-primary/60"
                  : "text-muted-foreground"
            }`}
            data-testid={`mobile-tab-${tab.id}`}
          >
            {isChat ? (
              <div className={`rounded-full p-1.5 transition-colors ${isActive ? "bg-primary/15" : ""}`}>
                <MessageSquare className="w-5 h-5" />
              </div>
            ) : (
              tab.icon
            )}
            <span className={`text-[10px] leading-tight ${isChat ? "font-medium" : ""}`}>{tab.label}</span>
            {badge > 0 && (
              <span className="absolute top-0.5 right-0 min-w-[16px] h-4 rounded-full bg-destructive text-[10px] font-medium flex items-center justify-center px-1 text-white">
                {badge > 99 ? "99+" : badge}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}
