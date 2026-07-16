"use client";

import { LayoutDashboard, ListTodo, Bot, MessageSquare, Store, ShieldAlert, Users, Activity, Workflow, HeartPulse, Sparkles, Zap } from "lucide-react";

export type ViewId = "dashboard" | "tasks" | "agents" | "marketplace" | "controls" | "fleet" | "runs" | "workflows" | "health" | "canvas";

interface NavItem {
  id: ViewId | "chat";
  icon: React.ReactNode;
  label: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const navGroups: NavGroup[] = [
  {
    label: "Workspace",
    items: [
      { id: "dashboard", icon: <LayoutDashboard className="w-4 h-4" />, label: "Dashboard" },
      { id: "tasks", icon: <ListTodo className="w-4 h-4" />, label: "Tasks" },
      { id: "agents", icon: <Bot className="w-4 h-4" />, label: "Agents" },
      { id: "marketplace", icon: <Store className="w-4 h-4" />, label: "Marketplace" },
    ],
  },
  {
    label: "Operator",
    items: [
      { id: "controls", icon: <ShieldAlert className="w-4 h-4" />, label: "Controls" },
      { id: "fleet", icon: <Users className="w-4 h-4" />, label: "Fleet" },
      { id: "runs", icon: <Activity className="w-4 h-4" />, label: "Runs" },
      { id: "workflows", icon: <Workflow className="w-4 h-4" />, label: "Workflows" },
      { id: "health", icon: <HeartPulse className="w-4 h-4" />, label: "Health" },
    ],
  },
  {
    label: "AI",
    items: [
      { id: "canvas", icon: <Sparkles className="w-4 h-4" />, label: "Canvas" },
    ],
  },
];

interface SidebarProps {
  activeView: ViewId;
  onViewChange: (view: ViewId) => void;
  chatOpen: boolean;
  onChatToggle: () => void;
  reviewCount: number;
  unhealthyCount: number;
}

export function Sidebar({
  activeView,
  onViewChange,
  chatOpen,
  onChatToggle,
  reviewCount,
  unhealthyCount,
}: SidebarProps) {
  const badgeCounts: Record<string, number> = {
    tasks: reviewCount,
    agents: unhealthyCount,
  };

  return (
    <nav
      className="hidden md:flex flex-col w-[196px] shrink-0 bg-sidebar border-r border-sidebar-border px-2.5 py-3 gap-0.5"
      data-testid="sidebar"
    >
      {/* Brand lockup — bolt on a gradient tile */}
      <div className="flex items-center gap-2.5 px-1.5 pb-3" data-testid="sidebar-bolt">
        <span
          aria-hidden
          className="flex size-6 shrink-0 items-center justify-center rounded-[7px] bg-gradient-to-br from-primary to-brand-2 text-white shadow-sm"
        >
          <Zap className="size-3.5" fill="currentColor" strokeWidth={0} />
        </span>
        <span className="text-sm font-semibold tracking-tight text-sidebar-foreground">
          Genus&thinsp;
          <span className="font-medium text-muted-foreground">OS</span>
        </span>
      </div>

      <div className="border-t border-sidebar-border mb-1" data-testid="sidebar-separator" />

      {navGroups.map((group) => (
        <div key={group.label} className="flex flex-col gap-0.5">
          <div className="px-1.5 pt-3 pb-1 text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted-foreground/70">
            {group.label}
          </div>
          {group.items.map((item) => {
            const isActive = activeView === item.id;
            const badge = badgeCounts[item.id] || 0;
            return (
              <button
                key={item.id}
                onClick={() => onViewChange(item.id as ViewId)}
                className={`relative flex items-center gap-2.5 rounded-md border px-2 py-1.5 text-[13px] transition-colors ${
                  isActive
                    ? "border-primary/25 bg-primary/10 text-sidebar-foreground"
                    : "border-transparent text-sidebar-foreground/65 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
                }`}
                data-testid={`nav-${item.id}`}
              >
                <span className={isActive ? "text-primary" : ""}>{item.icon}</span>
                {item.label}
                {badge > 0 && (
                  <span
                    className="ml-auto flex h-4 min-w-[16px] items-center justify-center rounded-full bg-destructive px-1 font-mono text-[10px] font-medium text-white"
                    data-testid={`badge-${item.id}`}
                  >
                    {badge > 99 ? "99+" : badge}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      ))}

      <div className="flex-1" />

      <div className="border-t border-sidebar-border mb-1" />

      {/* Chat toggle at bottom */}
      <button
        onClick={onChatToggle}
        className={`relative flex items-center gap-2.5 rounded-md border px-2 py-1.5 text-[13px] transition-colors ${
          chatOpen
            ? "border-primary/25 bg-primary/10 text-sidebar-foreground"
            : "border-transparent text-sidebar-foreground/65 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
        }`}
        data-testid="nav-chat"
      >
        <MessageSquare className={`w-4 h-4 ${chatOpen ? "text-primary" : ""}`} />
        Chat
      </button>
    </nav>
  );
}
