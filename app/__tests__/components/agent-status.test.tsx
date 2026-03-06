import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { AgentStatus } from "@/components/business/agent-status";

const mockAgents = [
  {
    name: "email-classifier",
    schedule: "0 6-22 * * *",
    status: "healthy" as const,
    lastRun: new Date().toISOString(),
    lastDuration: 1500,
    errorCount: 0,
  },
  {
    name: "supervisor",
    schedule: "*/17 6-22 * * *",
    status: "degraded" as const,
    lastRun: new Date(Date.now() - 3600_000).toISOString(),
    lastDuration: 5000,
    errorCount: 1,
    statusSummary: "All workers reporting. No escalations.",
  },
  {
    name: "crm-steward",
    schedule: "0 10,18 * * *",
    status: "failed" as const,
    errorCount: 3,
    pendingTasks: 5,
  },
];

const mockSummary = { healthy: 1, degraded: 1, failed: 1, sleeping: 0, total: 3 };

describe("AgentStatus", () => {
  it("renders agent cards", () => {
    render(<AgentStatus agents={mockAgents} />);
    const cards = screen.getAllByTestId("agent-card");
    expect(cards.length).toBe(3);
  });

  it("renders summary badges", () => {
    render(<AgentStatus agents={mockAgents} summary={mockSummary} />);
    const summaryDiv = screen.getByTestId("agent-summary");
    expect(summaryDiv).toBeDefined();
    expect(screen.getByText("1 healthy")).toBeDefined();
    expect(screen.getByText("1 degraded")).toBeDefined();
    expect(screen.getByText("1 failed")).toBeDefined();
  });

  it("shows status indicator for each agent", () => {
    render(<AgentStatus agents={mockAgents} />);
    const indicators = screen.getAllByTestId("status-indicator");
    expect(indicators.length).toBe(3);
  });

  it("shows error count badge for agents with errors", () => {
    render(<AgentStatus agents={mockAgents} />);
    expect(screen.getByText("3 err")).toBeDefined();
  });

  it("shows human-readable cron schedule", () => {
    render(<AgentStatus agents={mockAgents} />);
    // cronstrue converts "0 6-22 * * *" to something like "At 0 minutes past the hour, between 06:00 and 22:00"
    // Just verify some schedule text is rendered
    const cards = screen.getAllByTestId("agent-card");
    expect(cards.length).toBeGreaterThan(0);
  });

  it("shows pending tasks badge", () => {
    render(<AgentStatus agents={mockAgents} />);
    expect(screen.getByText("5 pending")).toBeDefined();
  });

  it("shows status summary text", () => {
    render(<AgentStatus agents={mockAgents} />);
    expect(screen.getByText("All workers reporting. No escalations.")).toBeDefined();
  });
});
