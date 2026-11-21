/**
 * The shell mounts live readers of the event stream (tasks, workspaces). In an
 * environment with no `EventSource` it must still mount without a single error
 * escaping into the page; the event stream itself is NOT mocked here.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ viewStack: [], pendingAgentData: null }),
  VisualStateProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/hooks/use-agents", () => ({
  useAgents: () => ({ agents: [], summary: { healthy: 0, degraded: 0, failed: 0, sleeping: 0, total: 0 }, isLoading: false }),
}));
vi.mock("@/hooks/use-inbox", () => ({
  useInbox: () => ({ items: [], count: 0, isLoading: false, error: null, refresh: vi.fn(), answer: vi.fn() }),
}));
vi.mock("@/hooks/use-accessible-tenants", () => ({ useHasChildTenants: () => false }));
vi.mock("@/components/layout/tenant-switcher", () => ({ TenantSwitcher: () => null, ActingTenantBanner: () => null }));
vi.mock("@/components/mfa-setup-banner", () => ({ MfaSetupBanner: () => null }));
vi.mock("@/components/canvas/live-canvas", () => ({ LiveCanvas: () => null }));
vi.mock("@/components/chat-panel", () => ({ ChatPanel: () => <div data-testid="chat-panel" /> }));

import { AppShell } from "@/components/layout/app-shell";

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("AppShell without EventSource", () => {
  it("mounts with live readers and lets no error escape", async () => {
    vi.stubGlobal("EventSource", undefined);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ rooms: [], data: { tasks: [] } })));
    const errors: unknown[] = [];
    const onProcess = (err: unknown) => errors.push(err);
    const onWindow = (event: ErrorEvent) => {
      errors.push(event.error ?? event.message);
      event.preventDefault();
    };
    process.on("uncaughtException", onProcess);
    window.addEventListener("error", onWindow);
    try {
      window.history.replaceState(null, "", "/?v=chat");
      const { unmount } = render(<AppShell />);
      expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
      await new Promise((r) => setTimeout(r, 50));
      unmount();
    } finally {
      process.off("uncaughtException", onProcess);
      window.removeEventListener("error", onWindow);
    }
    expect(errors).toEqual([]);
  });
});
