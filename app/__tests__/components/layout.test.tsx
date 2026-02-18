import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Mock dockview-react since it requires browser APIs
vi.mock("dockview-react", () => {
  const DockviewReact = ({
    onReady,
  }: {
    onReady: (event: unknown) => void;
    components: Record<string, React.FC>;
    className: string;
  }) => {
    const mockGroups = [
      { id: "group-1", api: { setSize: vi.fn() } },
      { id: "group-2", api: { setSize: vi.fn() } },
    ];

    const panels: { id: string; component: string; title: string }[] = [];

    const mockApi = {
      addPanel: vi.fn((config: { id: string; component: string; title: string }) => {
        panels.push(config);
        return config;
      }),
      groups: mockGroups,
      getGroup: (id: string) => mockGroups.find((g) => g.id === id),
    };

    if (onReady && panels.length === 0) {
      onReady({ api: mockApi });
    }

    return (
      <div data-testid="dockview-mock">
        {panels.map((p) => (
          <div key={p.id} data-testid={`panel-${p.id}`} data-title={p.title}>
            {p.component}
          </div>
        ))}
      </div>
    );
  };

  return { DockviewReact };
});

vi.mock("dockview-core/dist/styles/dockview.css", () => ({}));

// Mock child components
vi.mock("@/components/visual-panel", () => ({
  VisualPanel: () => <div data-testid="visual-panel">Visual Panel</div>,
}));

vi.mock("@/components/chat-panel", () => ({
  ChatPanel: () => <div data-testid="chat-panel">Chat Panel</div>,
}));

import { DockviewLayout } from "@/components/layout/dockview-layout";

describe("DockviewLayout", () => {
  it("renders dockview container", () => {
    render(<DockviewLayout />);
    expect(screen.getByTestId("dockview-container")).toBeInTheDocument();
  });

  it("creates visual and chat panels", () => {
    render(<DockviewLayout />);
    expect(screen.getByTestId("panel-visual")).toBeInTheDocument();
    expect(screen.getByTestId("panel-chat")).toBeInTheDocument();
  });

  it("creates exactly 2 panels", () => {
    render(<DockviewLayout />);
    const panels = screen.getAllByTestId(/^panel-/);
    expect(panels).toHaveLength(2);
  });

  it("visual panel has Dashboard title", () => {
    render(<DockviewLayout />);
    const visual = screen.getByTestId("panel-visual");
    expect(visual).toHaveAttribute("data-title", "Dashboard");
  });

  it("chat panel has Robothor title", () => {
    render(<DockviewLayout />);
    const chat = screen.getByTestId("panel-chat");
    expect(chat).toHaveAttribute("data-title", "Robothor");
  });
});
