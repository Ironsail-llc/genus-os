/**
 * F1 entry point: "New workspace with this agent" on each agent in the Agents
 * view, starting the dialog with that agent picked.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/views/agents/agent-manifests", () => ({ AgentManifests: () => null }));
vi.mock("@/hooks/use-agents", () => ({
  useAgents: () => ({ agents: [{ name: "Researcher", agentId: "researcher", status: "healthy", schedule: "" }] }),
}));

import { AgentsView } from "../agents-view";

const SUMMARY = { healthy: 1, degraded: 0, failed: 0, sleeping: 0, total: 1 };

describe("AgentsView — new workspace with this agent", () => {
  it("offers it on each agent, starting with that agent", async () => {
    render(
      <AgentsView
        visible
        agents={[{ name: "Researcher", agentId: "researcher", status: "healthy", schedule: "" }]}
        summary={SUMMARY}
        isLoading={false}
      />,
    );
    const button = screen.getByTestId("new-workspace-button");
    expect(button).toHaveTextContent("New workspace with this agent");
    fireEvent.click(button);
    expect(await screen.findByTestId("new-workspace-agent")).toHaveValue("researcher");
  });

  it("offers nothing for a health row with no agent id", () => {
    render(
      <AgentsView
        visible
        agents={[{ name: "Legacy", status: "healthy", schedule: "" }]}
        summary={SUMMARY}
        isLoading={false}
      />,
    );
    expect(screen.queryByTestId("new-workspace-button")).toBeNull();
  });
});
