/**
 * F1 entry point: "New workspace with this agent" on each agent in the Agents
 * view, starting the dialog with that agent picked.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/views/agents/agent-manifests", () => ({ AgentManifests: () => null }));
vi.mock("@/hooks/use-agents", () => ({
  useAgents: () => ({ agents: [{ name: "Researcher", agentId: "researcher", status: "healthy", schedule: "" }] }),
}));

import { AgentsView } from "../agents-view";

const SUMMARY = { healthy: 1, degraded: 0, failed: 0, sleeping: 0, total: 1 };

let roomsStatus = 200;
beforeEach(() => {
  roomsStatus = 200;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify(roomsStatus === 200 ? { rooms: [] } : { detail: "Not Found" }), {
        status: roomsStatus,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
});
afterEach(() => vi.unstubAllGlobals());

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
    const button = await screen.findByTestId("new-workspace-button");
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

  it("offers nothing where workspaces are not served", async () => {
    roomsStatus = 404;
    render(
      <AgentsView
        visible
        agents={[{ name: "Researcher", agentId: "researcher", status: "healthy", schedule: "" }]}
        summary={SUMMARY}
        isLoading={false}
      />,
    );
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId("new-workspace-button")).toBeNull();
  });
});

