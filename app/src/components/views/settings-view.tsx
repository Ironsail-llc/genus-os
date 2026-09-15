"use client";

import { Loader2, Lock } from "lucide-react";
import { PageHeader } from "@/components/business/page-header";
import { ThemeToggle } from "@/components/business/theme-toggle";
import { ErrorBoundary } from "@/components/error-boundary";
import { ControlsView } from "@/components/views/controls-view";
import { ChannelsPage } from "@/components/views/settings/channels-page";
import { ProvidersPage } from "@/components/views/settings/providers-page";
import { UsersPage } from "@/components/views/settings/users-page";
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
  /** The session has not resolved yet, so the role is unknown — not "denied". */
  roleLoading?: boolean;
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

function PageBody({ page, role }: { page: SettingsPage; role?: string | null }) {
  if (page.id === "providers") {
    // The first real settings page. It renders only inside this container, so
    // the role gate above is the only one it needs on the client — and the
    // bridge checks the caller's role on every provider route regardless.
    return <ProvidersPage />;
  }

  if (page.id === "channels") {
    // `visible` is not redundant with the container's own gate: this page
    // polls, and an inactive page must not. It is only ever mounted for the
    // active page of a visible Settings screen, so it is visible by
    // construction — the prop exists so the poll can be proved off in a test
    // and stays correct if this container ever starts keeping pages mounted.
    return <ChannelsPage visible />;
  }

  if (page.id === "users") {
    // The role travels because ownership cannot be handed over through the
    // API: only an owner may be offered the owner role. UX only — the bridge
    // refuses the same change independently, with a sentence this page prints.
    return <UsersPage visible role={role} />;
  }

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

/** What a sub-page that threw leaves behind — the rest of Settings still works. */
function PageFailed() {
  return (
    <div className="flex flex-col gap-2 p-4" data-testid="settings-page-error">
      <p className="text-sm font-medium text-foreground">This page failed to render</p>
      <p className="max-w-xl text-xs text-muted-foreground">
        Something in it threw while drawing — usually an answer from the engine in a shape this
        version of the dashboard does not know. Reload the Helm, or pick another page in the
        sidebar; nothing has been changed by the failure.
      </p>
    </div>
  );
}

export function SettingsView({
  visible,
  page,
  onPageChange,
  role,
  roleLoading,
}: SettingsViewProps) {
  if (!visible) return null;

  // Decide nothing until the session resolves: an owner deep-linking a settings
  // page must never be told the screen is not theirs while it is still loading.
  if (roleLoading) {
    return (
      <div
        className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center"
        data-testid="settings-loading"
      >
        <Loader2 aria-hidden className="size-5 animate-spin text-muted-foreground/60" />
        <p className="text-xs text-muted-foreground">Checking your access…</p>
      </div>
    );
  }

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
          {/*
            Keyed on the page so switching away from a broken one clears the
            failure: a boundary that latches would make one bad payload look
            like a broken Settings screen forever.
          */}
          <ErrorBoundary key={active.id} fallback={<PageFailed />}>
            <PageBody page={active} role={role} />
          </ErrorBoundary>
        </div>
      </div>
    </div>
  );
}
