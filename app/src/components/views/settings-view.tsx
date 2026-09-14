"use client";

import { Lock } from "lucide-react";
import { PageHeader } from "@/components/business/page-header";
import { ThemeToggle } from "@/components/business/theme-toggle";
import { ControlsView } from "@/components/views/controls-view";
import {
  isOperatorRole,
  settingsPages,
  type SettingsPage,
  type SettingsPageId,
} from "@/components/layout/nav-config";

interface SettingsViewProps {
  visible: boolean;
  page: SettingsPageId;
  onPageChange: (page: SettingsPageId) => void;
  /** Session role. UX gate only — the bridge authorizes every settings route itself. */
  role?: string | null;
}

/** Sub-navigation is grouped, not a tab strip: settings grow, tabs do not. */
function groupPages(pages: SettingsPage[]): Array<{ label: string; pages: SettingsPage[] }> {
  const groups: Array<{ label: string; pages: SettingsPage[] }> = [];
  for (const page of pages) {
    const existing = groups.find((g) => g.label === page.group);
    if (existing) existing.pages.push(page);
    else groups.push({ label: page.group, pages: [page] });
  }
  return groups;
}

function PageBody({ page }: { page: SettingsPage }) {
  if (page.id === "flags") {
    // The existing controls screen is the Flags page — same component, new home.
    return <ControlsView visible />;
  }

  return (
    <div className="flex flex-col gap-3 p-4" data-testid={`settings-page-${page.id}`}>
      <PageHeader title={page.label} />
      <p className="max-w-2xl text-xs text-muted-foreground">{page.description}</p>
      {page.id === "appearance" ? (
        <div className="flex max-w-2xl items-center gap-3 rounded-lg border border-border bg-card p-3">
          <span className="text-sm text-foreground">Theme</span>
          <span className="ml-auto">
            <ThemeToggle />
          </span>
        </div>
      ) : (
        <div className="max-w-2xl rounded-lg border border-dashed border-border bg-card/40 p-4">
          <p className="text-sm font-medium text-foreground">Coming soon</p>
          <p className="mt-1 text-xs text-muted-foreground">
            This page is a placeholder — the screen above describes what it will hold.
          </p>
        </div>
      )}
    </div>
  );
}

export function SettingsView({ visible, page, onPageChange, role }: SettingsViewProps) {
  if (!visible) return null;

  // UX gate only. Real enforcement is server-side and is NOT part of this task:
  // every settings route on the bridge checks the caller's role independently.
  if (!isOperatorRole(role)) {
    return (
      <div
        className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center"
        data-testid="settings-restricted"
      >
        <Lock aria-hidden className="size-6 text-muted-foreground/60" strokeWidth={1.5} />
        <p className="text-sm font-medium text-foreground">Settings are operator-only</p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Ask an owner or admin of this instance if you need something changed here.
        </p>
      </div>
    );
  }

  const active = settingsPages.find((p) => p.id === page) ?? settingsPages[0];

  return (
    <div className="flex h-full min-h-0" data-testid="settings-view">
      <nav
        aria-label="Settings"
        data-testid="settings-subnav"
        className="hidden w-[168px] shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-border p-2.5 md:flex"
      >
        {groupPages(settingsPages).map((group) => (
          <div key={group.label} data-testid={`settings-subgroup-${group.label.toLowerCase()}`}>
            <div className="px-1.5 pt-3 pb-1 text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted-foreground/70">
              {group.label}
            </div>
            {group.pages.map((p) => {
              const isActive = p.id === active.id;
              return (
                <button
                  key={p.id}
                  onClick={() => onPageChange(p.id)}
                  aria-current={isActive ? "page" : undefined}
                  data-testid={`settings-nav-${p.id}`}
                  className={`flex w-full items-center gap-2.5 rounded-md border px-2 py-1.5 text-[13px] transition-colors ${
                    isActive
                      ? "border-primary/25 bg-primary/10 text-foreground"
                      : "border-transparent text-foreground/65 hover:bg-accent/60 hover:text-foreground"
                  }`}
                >
                  <p.icon className={`size-4 ${isActive ? "text-primary" : ""}`} />
                  <span className="truncate">{p.label}</span>
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      {/* Narrow screens get the same pages as a scrollable chip row. */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="flex gap-1.5 overflow-x-auto border-b border-border p-2 md:hidden">
          {settingsPages.map((p) => (
            <button
              key={p.id}
              onClick={() => onPageChange(p.id)}
              aria-current={p.id === active.id ? "page" : undefined}
              data-testid={`settings-chip-${p.id}`}
              className={`shrink-0 rounded-full border px-2.5 py-1 text-xs transition-colors ${
                p.id === active.id
                  ? "border-primary/25 bg-primary/10 text-foreground"
                  : "border-border text-muted-foreground"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <PageBody page={active} />
        </div>
      </div>
    </div>
  );
}
