import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Sidebar } from "../sidebar";

function renderSidebar(overrides: Partial<React.ComponentProps<typeof Sidebar>> = {}) {
  const props: React.ComponentProps<typeof Sidebar> = {
    activeView: "chat",
    activeSettingsPage: "providers",
    onNavigate: vi.fn(),
    chatOpen: true,
    onChatToggle: vi.fn(),
    reviewCount: 0,
    unhealthyCount: 0,
    inboxCount: 0,
    role: "owner",
    ...overrides,
  };
  return { props, ...render(<Sidebar {...props} />) };
}

describe("Sidebar", () => {
  it("is a labelled nav landmark", () => {
    renderSidebar();
    const nav = screen.getByTestId("sidebar");
    expect(nav.tagName).toBe("NAV");
    expect(nav).toHaveAttribute("aria-label");
  });

  it("renders the four group headings", () => {
    renderSidebar();
    for (const label of ["Chat", "Workspace", "Observe", "Settings"]) {
      expect(screen.getByTestId(`nav-group-${label.toLowerCase()}`).textContent).toBe(label);
    }
  });

  it("marks the active item with aria-current", () => {
    renderSidebar({ activeView: "runs" });
    expect(screen.getByTestId("nav-runs")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("nav-fleet")).not.toHaveAttribute("aria-current");
  });

  it("marks the active settings sub-page with aria-current", () => {
    renderSidebar({ activeView: "settings", activeSettingsPage: "secrets" });
    expect(screen.getByTestId("nav-settings-secrets")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("nav-settings-providers")).not.toHaveAttribute("aria-current");
  });

  it("navigates on click", () => {
    const onNavigate = vi.fn();
    renderSidebar({ onNavigate });
    fireEvent.click(screen.getByTestId("nav-health"));
    expect(onNavigate).toHaveBeenCalledWith("health", undefined);
  });

  it("navigates to a settings sub-page on click", () => {
    const onNavigate = vi.fn();
    renderSidebar({ onNavigate });
    fireEvent.click(screen.getByTestId("nav-settings-flags"));
    expect(onNavigate).toHaveBeenCalledWith("settings", "flags");
  });

  it("reaches Memory, Audit and Logs — built screens, no soon pill", () => {
    const onNavigate = vi.fn();
    renderSidebar({ onNavigate });
    for (const id of ["memory", "audit", "logs"]) {
      const item = screen.getByTestId(`nav-${id}`);
      expect(item).not.toBeDisabled();
      expect(screen.queryByTestId(`soon-${id}`)).toBeNull();
    }
    fireEvent.click(screen.getByTestId("nav-memory"));
    expect(onNavigate).toHaveBeenCalledWith("memory", undefined);
  });

  /**
   * The gate is per item, not per group: taking Observe away from a member to
   * protect Memory would take Runs, Fleet and Health away with it.
   */
  it("shows an auditor Audit and hides Memory and Logs", () => {
    renderSidebar({ role: "auditor" });
    expect(screen.getByTestId("nav-audit")).toBeTruthy();
    expect(screen.queryByTestId("nav-memory")).toBeNull();
    expect(screen.queryByTestId("nav-logs")).toBeNull();
    expect(screen.getByTestId("nav-runs")).toBeTruthy();
  });

  it("shows a member none of the three, and the rest of Observe anyway", () => {
    renderSidebar({ role: "member" });
    for (const id of ["memory", "audit", "logs"]) {
      expect(screen.queryByTestId(`nav-${id}`)).toBeNull();
    }
    expect(screen.getByTestId("nav-group-observe")).toBeTruthy();
    expect(screen.getByTestId("nav-health")).toBeTruthy();
  });

  it("reaches the Inbox, which is built and no longer wears a soon pill", () => {
    const onNavigate = vi.fn();
    renderSidebar({ onNavigate });
    const item = screen.getByTestId("nav-inbox");
    expect(item).not.toBeDisabled();
    expect(screen.queryByTestId("soon-inbox")).toBeNull();
    fireEvent.click(item);
    expect(onNavigate).toHaveBeenCalledWith("inbox", undefined);
  });

  it("hides the Settings group from non-operator roles", () => {
    renderSidebar({ role: "viewer" });
    expect(screen.queryByTestId("nav-group-settings")).toBeNull();
    expect(screen.queryByTestId("nav-settings-providers")).toBeNull();
  });

  it("hides the Settings group when no role is known", () => {
    renderSidebar({ role: undefined });
    expect(screen.queryByTestId("nav-group-settings")).toBeNull();
  });

  it("still badges tasks and agents", () => {
    renderSidebar({ reviewCount: 3, unhealthyCount: 120 });
    expect(screen.getByTestId("badge-tasks").textContent).toBe("3");
    expect(screen.getByTestId("badge-agents").textContent).toBe("99+");
  });

  it("badges the Inbox with what is waiting on a person", () => {
    renderSidebar({ inboxCount: 2 });
    expect(screen.getByTestId("badge-inbox").textContent).toBe("2");
  });

  it("drops the Inbox badge when nothing is waiting", () => {
    renderSidebar({ inboxCount: 0 });
    expect(screen.queryByTestId("badge-inbox")).toBeNull();
  });

  it("keeps a docked chat panel toggle", () => {
    const onChatToggle = vi.fn();
    renderSidebar({ onChatToggle });
    fireEvent.click(screen.getByTestId("chat-panel-toggle"));
    expect(onChatToggle).toHaveBeenCalled();
  });
});
