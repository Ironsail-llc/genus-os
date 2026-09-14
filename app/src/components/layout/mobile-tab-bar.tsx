"use client";

import { useEffect, useRef, useState } from "react";
import { Bot, Inbox, MessageSquare, MoreHorizontal, X } from "lucide-react";
import {
  visibleNavGroups,
  type NavItem,
  type SettingsPageId,
  type ViewId,
} from "./nav-config";

/**
 * Four slots fit a phone bottom bar. Chat, Inbox and Agents are the three the
 * operator reaches for; everything else — including the whole Settings tree —
 * lives behind More, which opens a sheet built from the same nav config the
 * sidebar uses.
 */
const TABS: Array<{
  id: string;
  label: string;
  view: ViewId;
  icon: typeof Bot;
  soon?: boolean;
}> = [
  { id: "chat", label: "Chat", view: "chat", icon: MessageSquare },
  { id: "inbox", label: "Inbox", view: "inbox", icon: Inbox },
  { id: "agents", label: "Agents", view: "agents", icon: Bot },
];

interface MobileTabBarProps {
  activeView: ViewId;
  activeSettingsPage: SettingsPageId;
  onNavigate: (view: ViewId, settingsPage?: SettingsPageId) => void;
  reviewCount: number;
  unhealthyCount: number;
  /** What is waiting on a person, from the one inbox poll the shell holds. */
  inboxCount: number;
  /** Session role — hides Settings in the sheet. UX gate only; the bridge authorizes. */
  role?: string | null;
}

const TAB_CLASS =
  "relative flex flex-col items-center justify-center min-w-[44px] min-h-[44px] gap-0.5 transition-colors";

export function MobileTabBar({
  activeView,
  activeSettingsPage,
  onNavigate,
  reviewCount,
  unhealthyCount,
  inboxCount,
  role,
}: MobileTabBarProps) {
  const [moreOpen, setMoreOpen] = useState(false);
  const sheetRef = useRef<HTMLDivElement | null>(null);
  const moreButtonRef = useRef<HTMLButtonElement | null>(null);

  // The sheet is a modal dialog, so it owns the keyboard while it is open:
  // Escape closes it, Tab cycles inside it, and focus goes back where it came
  // from on close. The bar behind it is inert so nothing there is reachable.
  useEffect(() => {
    if (!moreOpen) return;

    // Whatever opened the sheet gets focus back; when nothing held focus (the
    // sheet was opened by a pointer), that is the More button itself.
    const active = document.activeElement as HTMLElement | null;
    const opener = active && active !== document.body ? active : moreButtonRef.current;
    const focusable = () =>
      Array.from(sheetRef.current?.querySelectorAll<HTMLElement>("button:not([disabled])") ?? []);

    focusable()[0]?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMoreOpen(false);
        return;
      }
      if (event.key !== "Tab") return;

      const items = focusable();
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      const inside = sheetRef.current?.contains(active ?? null) ?? false;

      if (event.shiftKey && (!inside || active === first)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (!inside || active === last)) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      opener?.focus?.();
    };
  }, [moreOpen]);

  // Inbox and Agents carry badges in the bar itself; the review queue badges
  // More, which is where Tasks now lives.
  const badgeCounts: Record<string, number> = { inbox: inboxCount, agents: unhealthyCount };

  const inTabBar = new Set(TABS.map((t) => t.view));
  const sheetGroups = visibleNavGroups(role)
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => !inTabBar.has(item.view) || item.sub),
    }))
    .filter((group) => group.items.length > 0);

  const go = (item: Pick<NavItem, "view" | "sub">) => {
    onNavigate(item.view, item.sub);
    setMoreOpen(false);
  };

  return (
    <>
      {moreOpen && (
        <div
          className="fixed inset-0 z-50 flex flex-col justify-end bg-scrim backdrop-blur-[2px]"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setMoreOpen(false);
          }}
        >
          <div
            ref={sheetRef}
            role="dialog"
            aria-modal="true"
            aria-label="More navigation"
            data-testid="mobile-more-sheet"
            className="max-h-[75vh] overflow-y-auto rounded-t-2xl border-t border-border bg-popover pb-14"
          >
            <div className="sticky top-0 flex items-center gap-2 border-b border-border bg-popover px-4 py-3">
              <span className="text-sm font-semibold text-foreground">More</span>
              <button
                onClick={() => setMoreOpen(false)}
                aria-label="Close"
                data-testid="mobile-more-close"
                className="ml-auto flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-accent"
              >
                <X className="size-4" />
              </button>
            </div>
            <div className="p-2">
              {sheetGroups.map((group) => (
                <div key={group.id} className="mb-2">
                  <div className="px-2 pt-2 pb-1 text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted-foreground/70">
                    {group.label}
                  </div>
                  {group.items.map((item) => {
                    const active =
                      item.view === activeView && (!item.sub || item.sub === activeSettingsPage);
                    return (
                      <button
                        key={item.id}
                        onClick={() => go(item)}
                        disabled={item.soon}
                        aria-disabled={item.soon ? true : undefined}
                        aria-current={active ? "page" : undefined}
                        data-testid={`more-nav-${item.id}`}
                        className={`flex min-h-[44px] w-full items-center gap-3 rounded-md px-2 text-sm transition-colors ${
                          item.soon
                            ? "text-foreground/35"
                            : active
                              ? "bg-primary/10 text-foreground"
                              : "text-foreground/80 hover:bg-accent"
                        }`}
                      >
                        <item.icon className={`size-4 ${active && !item.soon ? "text-primary" : ""}`} />
                        {item.label}
                        {item.soon && (
                          <span
                            data-testid={`more-soon-${item.id}`}
                            className="ml-auto rounded-full border border-border bg-muted px-1.5 text-[9.5px] font-medium uppercase tracking-wide text-muted-foreground"
                          >
                            soon
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      <nav
        aria-label="Primary"
        aria-hidden={moreOpen || undefined}
        inert={moreOpen}
        className="fixed bottom-0 left-0 right-0 z-50 flex items-center justify-around h-14 border-t border-border bg-background/95 backdrop-blur-md safe-area-bottom"
        data-testid="mobile-tab-bar"
      >
        {TABS.map((tab) => {
          const isActive = activeView === tab.view;
          const isChat = tab.id === "chat";
          const badge = badgeCounts[tab.id] || 0;
          return (
            <button
              key={tab.id}
              onClick={() => onNavigate(tab.view, undefined)}
              aria-current={isActive ? "page" : undefined}
              className={`${TAB_CLASS} ${
                isActive
                  ? "text-primary"
                  : isChat
                    ? "text-primary/60"
                    : tab.soon
                      ? "text-muted-foreground/60"
                      : "text-muted-foreground"
              }`}
              data-testid={`mobile-tab-${tab.id}`}
            >
              {isChat ? (
                <div className={`rounded-full p-1.5 transition-colors ${isActive ? "bg-primary/15" : ""}`}>
                  <tab.icon className="w-5 h-5" />
                </div>
              ) : (
                <tab.icon className="w-5 h-5" />
              )}
              <span className={`text-[10px] leading-tight ${isChat ? "font-medium" : ""}`}>
                {tab.label}
              </span>
              {tab.soon && (
                <span
                  data-testid={`mobile-soon-${tab.id}`}
                  className="absolute top-0.5 right-0 rounded-full border border-border bg-muted px-1 text-[8px] font-medium uppercase tracking-wide text-muted-foreground"
                >
                  soon
                </span>
              )}
              {badge > 0 && (
                <span
                  data-testid={`badge-${tab.id}`}
                  className="absolute top-0.5 right-0 min-w-[16px] h-4 rounded-full bg-destructive text-[10px] font-medium flex items-center justify-center px-1 text-white"
                >
                  {badge > 99 ? "99+" : badge}
                </span>
              )}
            </button>
          );
        })}

        <button
          ref={moreButtonRef}
          onClick={() => setMoreOpen((prev) => !prev)}
          aria-expanded={moreOpen}
          aria-haspopup="dialog"
          className={`${TAB_CLASS} ${moreOpen ? "text-primary" : "text-muted-foreground"}`}
          data-testid="mobile-tab-more"
        >
          <MoreHorizontal className="w-5 h-5" />
          <span className="text-[10px] leading-tight">More</span>
          {reviewCount > 0 && (
            <span
              data-testid="badge-more"
              className="absolute top-0.5 right-0 min-w-[16px] h-4 rounded-full bg-destructive text-[10px] font-medium flex items-center justify-center px-1 text-white"
            >
              {reviewCount > 99 ? "99+" : reviewCount}
            </span>
          )}
        </button>
      </nav>
    </>
  );
}
