"use client";

import { LayoutDashboard, ListTodo, Bot, MessageSquare, Store } from "lucide-react";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import Image from "next/image";

export type ViewId = "dashboard" | "tasks" | "agents" | "marketplace";

interface NavItem {
  id: ViewId | "chat";
  icon: React.ReactNode;
  label: string;
}

const navItems: NavItem[] = [
  { id: "dashboard", icon: <LayoutDashboard className="w-5 h-5" />, label: "Dashboard" },
  { id: "tasks", icon: <ListTodo className="w-5 h-5" />, label: "Tasks" },
  { id: "agents", icon: <Bot className="w-5 h-5" />, label: "Agents" },
  { id: "marketplace", icon: <Store className="w-5 h-5" />, label: "Marketplace" },
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
      className="hidden md:flex flex-col items-center w-12 shrink-0 bg-sidebar border-r border-sidebar-border py-3 gap-1"
      data-testid="sidebar"
    >
      {/* Brand bolt */}
      <div className="mb-2" data-testid="sidebar-bolt">
        <Image
          src="/robothor-bolt.svg"
          alt="Genus OS"
          width={20}
          height={20}
          className="opacity-80"
        />
      </div>

      <div className="w-6 border-t border-sidebar-border mb-1" data-testid="sidebar-separator" />

      {navItems.map((item) => {
        const isActive = activeView === item.id;
        const badge = badgeCounts[item.id] || 0;
        return (
          <Tooltip key={item.id}>
            <TooltipTrigger asChild>
              <button
                onClick={() => onViewChange(item.id as ViewId)}
                className={`relative flex items-center justify-center w-9 h-9 rounded-md transition-colors ${
                  isActive
                    ? "bg-sidebar-accent text-sidebar-accent-foreground border-l-2 border-l-primary"
                    : "text-sidebar-foreground/60 hover:text-sidebar-foreground hover:bg-sidebar-accent/50"
                }`}
                data-testid={`nav-${item.id}`}
              >
                {item.icon}
                {badge > 0 && (
                  <span
                    className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 rounded-full bg-destructive text-[10px] font-medium flex items-center justify-center px-1 text-white"
                    data-testid={`badge-${item.id}`}
                  >
                    {badge > 99 ? "99+" : badge}
                  </span>
                )}
              </button>
            </TooltipTrigger>
            <TooltipContent side="right" sideOffset={8}>
              {item.label}
            </TooltipContent>
          </Tooltip>
        );
      })}

      <div className="flex-1" />

      <div className="w-6 border-t border-sidebar-border mb-1" />

      {/* Chat toggle at bottom */}
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            onClick={onChatToggle}
            className={`relative flex items-center justify-center w-9 h-9 rounded-md transition-colors ${
              chatOpen
                ? "bg-sidebar-accent text-sidebar-accent-foreground"
                : "text-sidebar-foreground/60 hover:text-sidebar-foreground hover:bg-sidebar-accent/50"
            }`}
            data-testid="nav-chat"
          >
            <MessageSquare className="w-5 h-5" />
          </button>
        </TooltipTrigger>
        <TooltipContent side="right" sideOffset={8}>
          Chat
        </TooltipContent>
      </Tooltip>
    </nav>
  );
}
