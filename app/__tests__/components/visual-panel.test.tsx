import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

// Mock Recharts
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  BarChart: () => null,
  Bar: () => null,
  LineChart: () => null,
  Line: () => null,
  PieChart: () => null,
  Pie: () => null,
  Cell: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

// Mock API clients
vi.mock("@/lib/api/health", () => ({
  fetchHealth: vi.fn().mockResolvedValue({
    status: "ok",
    services: [
      { name: "bridge", url: "http://localhost:9100/health", status: "healthy", responseTime: 5 },
    ],
    timestamp: "2026-01-01T00:00:00Z",
  }),
}));

// Mock sandpack to prevent heavy imports
vi.mock("@codesandbox/sandpack-react", () => ({
  SandpackProvider: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  SandpackPreview: () => <div data-testid="sandpack-preview">Preview</div>,
}));

import { VisualStateProvider } from "@/hooks/use-visual-state";
import { VisualPanel } from "@/components/visual-panel";

function renderWithProvider(ui: React.ReactNode) {
  return render(<VisualStateProvider>{ui}</VisualStateProvider>);
}

describe("VisualPanel", () => {
  it("renders visual panel container", () => {
    renderWithProvider(<VisualPanel />);
    expect(screen.getByTestId("visual-panel")).toBeInTheDocument();
  });

  it("renders live canvas", () => {
    renderWithProvider(<VisualPanel />);
    expect(screen.getByTestId("live-canvas")).toBeInTheDocument();
  });
});
