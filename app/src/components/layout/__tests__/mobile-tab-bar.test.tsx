import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { MobileTabBar } from "../mobile-tab-bar";

function renderBar(overrides: Partial<React.ComponentProps<typeof MobileTabBar>> = {}) {
  const props: React.ComponentProps<typeof MobileTabBar> = {
    activeView: "chat",
    activeSettingsPage: "providers",
    onNavigate: vi.fn(),
    reviewCount: 0,
    unhealthyCount: 0,
    role: "owner",
    ...overrides,
  };
  return { props, ...render(<MobileTabBar {...props} />) };
}

describe("MobileTabBar", () => {
  it("shows Chat, Inbox, Agents and More in that order", () => {
    renderBar();
    const tabs = screen.getByTestId("mobile-tab-bar").querySelectorAll("button");
    expect([...tabs].map((t) => t.getAttribute("data-testid"))).toEqual([
      "mobile-tab-chat",
      "mobile-tab-inbox",
      "mobile-tab-agents",
      "mobile-tab-more",
    ]);
  });

  it("is a labelled nav landmark", () => {
    renderBar();
    const nav = screen.getByTestId("mobile-tab-bar");
    expect(nav.tagName).toBe("NAV");
    expect(nav).toHaveAttribute("aria-label");
  });

  it("marks the active tab with aria-current", () => {
    renderBar({ activeView: "agents" });
    expect(screen.getByTestId("mobile-tab-agents")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("mobile-tab-chat")).not.toHaveAttribute("aria-current");
  });

  it("keeps 44px touch targets", () => {
    renderBar();
    const tabs = screen.getByTestId("mobile-tab-bar").querySelectorAll("button");
    for (const tab of tabs) {
      expect(tab.className).toContain("min-w-[44px]");
      expect(tab.className).toContain("min-h-[44px]");
    }
  });

  it("routes the Inbox tab to the placeholder view rather than nowhere", () => {
    const onNavigate = vi.fn();
    renderBar({ onNavigate });
    fireEvent.click(screen.getByTestId("mobile-tab-inbox"));
    expect(onNavigate).toHaveBeenCalledWith("inbox", undefined);
  });

  it("opens a sheet of everything else behind More", () => {
    renderBar();
    expect(screen.queryByTestId("mobile-more-sheet")).toBeNull();
    fireEvent.click(screen.getByTestId("mobile-tab-more"));
    const sheet = screen.getByTestId("mobile-more-sheet");
    expect(within(sheet).getByTestId("more-nav-dashboard")).toBeInTheDocument();
    expect(within(sheet).getByTestId("more-nav-runs")).toBeInTheDocument();
    expect(within(sheet).getByText("Observe")).toBeInTheDocument();
  });

  it("navigates from the sheet and closes it", () => {
    const onNavigate = vi.fn();
    renderBar({ onNavigate });
    fireEvent.click(screen.getByTestId("mobile-tab-more"));
    fireEvent.click(screen.getByTestId("more-nav-dashboard"));
    expect(onNavigate).toHaveBeenCalledWith("dashboard", undefined);
    expect(screen.queryByTestId("mobile-more-sheet")).toBeNull();
  });

  it("disables not-yet-built entries in the sheet", () => {
    renderBar();
    fireEvent.click(screen.getByTestId("mobile-tab-more"));
    const item = screen.getByTestId("more-nav-memory");
    expect(item).toBeDisabled();
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByTestId("more-soon-memory").textContent?.toLowerCase()).toBe("soon");
  });

  it("hides Settings in the sheet from non-operator roles", () => {
    renderBar({ role: "viewer" });
    fireEvent.click(screen.getByTestId("mobile-tab-more"));
    expect(screen.queryByTestId("more-nav-settings-providers")).toBeNull();
    fireEvent.click(screen.getByTestId("mobile-more-close"));
    expect(screen.queryByTestId("mobile-more-sheet")).toBeNull();
  });

  it("shows Settings in the sheet for an owner", () => {
    renderBar();
    fireEvent.click(screen.getByTestId("mobile-tab-more"));
    expect(screen.getByTestId("more-nav-settings-providers")).toBeInTheDocument();
  });

  it("badges agents with the unhealthy count", () => {
    renderBar({ unhealthyCount: 2 });
    expect(screen.getByTestId("badge-agents").textContent).toBe("2");
  });

  it("badges More with the review queue, which now lives behind it", () => {
    renderBar({ reviewCount: 4 });
    expect(screen.getByTestId("badge-more").textContent).toBe("4");
  });
});
