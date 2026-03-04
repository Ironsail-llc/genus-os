"use client";

import { MetricGrid } from "@/components/business/metric-grid";
import { AgentStatus } from "@/components/business/agent-status";
import type { AgentInfo, AgentSummary } from "@/hooks/use-agents";

interface AgentsViewProps {
  visible: boolean;
  agents: AgentInfo[];
  summary: AgentSummary;
  isLoading: boolean;
}

export function AgentsView({ visible, agents, summary, isLoading }: AgentsViewProps) {
  const metrics = [
    { title: "Healthy", value: summary.healthy },
    { title: "Sleeping", value: summary.sleeping },
    { title: "Degraded", value: summary.degraded },
    { title: "Failed", value: summary.failed },
    { title: "Total Agents", value: summary.total },
  ];

  return (
    <div
      className="h-full w-full flex flex-col overflow-y-auto"
      style={{ display: visible ? "flex" : "none" }}
      data-testid="agents-view"
    >
      <div className="p-4 space-y-4">
        <MetricGrid metrics={metrics} />
        {isLoading && agents.length === 0 ? (
          <div className="flex items-center justify-center py-12 text-muted-foreground text-sm">
            Loading agents...
          </div>
        ) : (
          <AgentStatus agents={agents} summary={summary} />
        )}
      </div>
    </div>
  );
}
