"use client";

import { MessageSquare, Zap } from "lucide-react";
import {
  visibleNavGroups,
  type NavItem,
  type SettingsPageId,
  type ViewId,
} from "./nav-config";

interface SidebarProps {
  activeView: ViewId;
  activeSettingsPage: SettingsPageId;
  onNavigate: (view: ViewId, settingsPage?: SettingsPageId) => void;
  chatOpen: boolean;
  onChatToggle: () => void;
  reviewCount: number;
  unhealthyCount: number;
  /**
   * The signed-in role, straight from the session the auth layer already
   * exposes. This hides Settings from non-operators — a UX gate only. Real
   * authorization is server-side and is NOT part of this component: the bridge
   * checks the caller's role on every settings route.
   */
  role?: string | null;
}

function SoonPill({ id }: { id: string }) {
  return (
    <span
      data-testid={`soon-${id}`}
      className="ml-auto rounded-full border border-border bg-muted px-1.5 text-[9.5px] font-medium uppercase tracking-wide text-muted-foreground"
    >
      soon
    </span>
  );
}

export function Sidebar({
  activeView,
  activeSettingsPage,
  onNavigate,
  chatOpen,
  onChatToggle,
  reviewCount,
  unhealthyCount,
  role,
}: SidebarProps) {
  const badgeCounts: Record<string, number> = {
    tasks: reviewCount,
    agents: unhealthyCount,
  };

  const isActive = (item: NavItem) =>
    item.view === activeView && (!item.sub || item.sub === activeSettingsPage);

  return (
    <nav
      aria-label="Primary"
      className="hidden md:flex flex-col w-[196px] shrink-0 bg-sidebar border-r border-sidebar-border px-2.5 py-3 gap-0.5 overflow-y-auto"
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

      {visibleNavGroups(role).map((group) => (
        <div key={group.id} className="flex flex-col gap-0.5">
          <div
            data-testid={`nav-group-${group.id}`}
            className="px-1.5 pt-3 pb-1 text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted-foreground/70"
          >
            {group.label}
          </div>
          {group.items.map((item) => {
            const active = isActive(item);
            const badge = badgeCounts[item.id] || 0;
            return (
              <button
                key={item.id}
                onClick={() => onNavigate(item.view, item.sub)}
                disabled={item.soon}
                aria-disabled={item.soon ? true : undefined}
                aria-current={active ? "page" : undefined}
                className={`relative flex items-center gap-2.5 rounded-md border px-2 py-1.5 text-[13px] transition-colors ${
                  item.soon
                    ? "border-transparent text-sidebar-foreground/35 cursor-not-allowed"
                    : active
                      ? "border-primary/25 bg-primary/10 text-sidebar-foreground"
                      : "border-transparent text-sidebar-foreground/65 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
                }`}
                data-testid={`nav-${item.id}`}
              >
                <item.icon className={`w-4 h-4 ${active && !item.soon ? "text-primary" : ""}`} />
                <span className="truncate">{item.label}</span>
                {item.soon && <SoonPill id={item.id} />}
                {!item.soon && badge > 0 && (
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

      {/* Docked chat panel — shown alongside any non-chat view. */}
      <button
        onClick={onChatToggle}
        aria-pressed={chatOpen}
        className={`relative flex items-center gap-2.5 rounded-md border px-2 py-1.5 text-[13px] transition-colors ${
          chatOpen
            ? "border-primary/25 bg-primary/10 text-sidebar-foreground"
            : "border-transparent text-sidebar-foreground/65 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
        }`}
        data-testid="chat-panel-toggle"
      >
        <MessageSquare className={`w-4 h-4 ${chatOpen ? "text-primary" : ""}`} />
        Chat panel
      </button>
    </nav>
  );
}
