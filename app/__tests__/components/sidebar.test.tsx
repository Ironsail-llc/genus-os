import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Sidebar, type ViewId } from "@/components/layout/sidebar";

// Mock next/image
vi.mock("next/image", () => ({
  default: (props: Record<string, unknown>) => <img alt="" {...props} />,
}));

// Mock shadcn tooltip (render children directly)
vi.mock("@/components/ui/tooltip", () => ({
  Tooltip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipContent: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

function renderSidebar(overrides: Partial<React.ComponentProps<typeof Sidebar>> = {}) {
  const defaults = {
    activeView: "dashboard" as ViewId,
    onViewChange: vi.fn(),
    chatOpen: false,
    onChatToggle: vi.fn(),
    reviewCount: 0,
    unhealthyCount: 0,
    ...overrides,
  };
  return { ...render(<Sidebar {...defaults} />), ...defaults };
}

describe("Sidebar", () => {
  it("renders all 4 nav icons", () => {
    renderSidebar();
    expect(screen.getByTestId("nav-dashboard")).toBeInTheDocument();
    expect(screen.getByTestId("nav-tasks")).toBeInTheDocument();
    expect(screen.getByTestId("nav-agents")).toBeInTheDocument();
    expect(screen.getByTestId("nav-chat")).toBeInTheDocument();
  });

  it("renders bolt icon and separator", () => {
    renderSidebar();
    expect(screen.getByTestId("sidebar-bolt")).toBeInTheDocument();
    expect(screen.getByTestId("sidebar-separator")).toBeInTheDocument();
  });

  it("highlights active view with accent border", () => {
    renderSidebar({ activeView: "tasks" });
    const tasksBtn = screen.getByTestId("nav-tasks");
    expect(tasksBtn.className).toContain("bg-primary/10");
    expect(tasksBtn.className).toContain("border-primary/25");
    const dashboardBtn = screen.getByTestId("nav-dashboard");
    expect(dashboardBtn.className).not.toContain("bg-primary/10");
  });

  it("calls onViewChange when nav item clicked", () => {
    const { onViewChange } = renderSidebar();
    fireEvent.click(screen.getByTestId("nav-agents"));
    expect(onViewChange).toHaveBeenCalledWith("agents");
  });

  it("calls onChatToggle when chat icon clicked", () => {
    const { onChatToggle } = renderSidebar();
    fireEvent.click(screen.getByTestId("nav-chat"));
    expect(onChatToggle).toHaveBeenCalledOnce();
  });

  it("shows task review badge when reviewCount > 0", () => {
    renderSidebar({ reviewCount: 3 });
    const badge = screen.getByTestId("badge-tasks");
    expect(badge).toBeInTheDocument();
    expect(badge.textContent).toBe("3");
  });

  it("shows agent unhealthy badge when unhealthyCount > 0", () => {
    renderSidebar({ unhealthyCount: 2 });
    const badge = screen.getByTestId("badge-agents");
    expect(badge).toBeInTheDocument();
    expect(badge.textContent).toBe("2");
  });

  it("does not show badges when counts are 0", () => {
    renderSidebar({ reviewCount: 0, unhealthyCount: 0 });
    expect(screen.queryByTestId("badge-tasks")).not.toBeInTheDocument();
    expect(screen.queryByTestId("badge-agents")).not.toBeInTheDocument();
  });

  it("caps badge display at 99+", () => {
    renderSidebar({ reviewCount: 150 });
    expect(screen.getByTestId("badge-tasks").textContent).toBe("99+");
  });

  it("highlights chat icon when chatOpen", () => {
    renderSidebar({ chatOpen: true });
    const chatBtn = screen.getByTestId("nav-chat");
    expect(chatBtn.className).toContain("bg-primary/10");
  });
});
