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

  it("renders views that do not exist yet as disabled items with a soon pill", () => {
    const onNavigate = vi.fn();
    renderSidebar({ onNavigate });
    for (const id of ["inbox", "memory", "audit", "logs"]) {
      const item = screen.getByTestId(`nav-${id}`);
      expect(item).toBeDisabled();
      expect(item).toHaveAttribute("aria-disabled", "true");
      expect(screen.getByTestId(`soon-${id}`).textContent?.toLowerCase()).toBe("soon");
    }
    fireEvent.click(screen.getByTestId("nav-inbox"));
    expect(onNavigate).not.toHaveBeenCalled();
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

  it("keeps a docked chat panel toggle", () => {
    const onChatToggle = vi.fn();
    renderSidebar({ onChatToggle });
    fireEvent.click(screen.getByTestId("chat-panel-toggle"));
    expect(onChatToggle).toHaveBeenCalled();
  });
});
