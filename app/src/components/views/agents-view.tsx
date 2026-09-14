"use client";

import { useState, useMemo } from "react";
import { MetricGrid } from "@/components/business/metric-grid";
import { AgentStatus } from "@/components/business/agent-status";
import { AgentManifests } from "@/components/views/agents/agent-manifests";
import type { AgentInfo, AgentSummary } from "@/hooks/use-agents";

type SortMode = "score" | "health" | "name";

interface AgentsViewProps {
  visible: boolean;
  agents: AgentInfo[];
  summary: AgentSummary;
  isLoading: boolean;
  /**
   * Session role, for the manifest builder below the health cards. UX gate
   * only — the bridge authorizes every agent-manifest route itself.
   */
  role?: string | null;
  /** The session has not resolved yet, so the role is unknown — not "denied". */
  roleLoading?: boolean;
}

const tierOrder: Record<string, number> = {
  failed: 0,
  degraded: 1,
  unknown: 2,
  healthy: 3,
  sleeping: 4,
};

export function AgentsView({
  visible,
  agents,
  summary,
  isLoading,
  role,
  roleLoading,
}: AgentsViewProps) {
  const [sortBy, setSortBy] = useState<SortMode>("score");

  const sortedAgents = useMemo(() => {
    const sorted = [...agents];
    switch (sortBy) {
      case "score":
        sorted.sort((a, b) => (b.rpg?.overall ?? -1) - (a.rpg?.overall ?? -1));
        break;
      case "health":
        sorted.sort((a, b) => (tierOrder[a.status] ?? 2) - (tierOrder[b.status] ?? 2));
        break;
      case "name":
        sorted.sort((a, b) => a.name.localeCompare(b.name));
        break;
    }
    return sorted;
  }, [agents, sortBy]);

  // Fleet RPG averages
  const fleetRpg = useMemo(() => {
    const withScores = agents.filter((a) => a.rpg);
    if (withScores.length === 0) return null;
    const avgOverall = Math.round(
      withScores.reduce((sum, a) => sum + (a.rpg?.overall ?? 0), 0) / withScores.length
    );
    const above50 = withScores.filter((a) => (a.rpg?.overall ?? 0) >= 50).length;
    return { avgOverall, above50, below50: withScores.length - above50, total: withScores.length };
  }, [agents]);

  const metrics = [
    { title: "Healthy", value: summary.healthy },
    { title: "Sleeping", value: summary.sleeping },
    { title: "Degraded", value: summary.degraded },
    { title: "Failed", value: summary.failed },
    { title: "Total Agents", value: summary.total },
    ...(fleetRpg ? [{ title: "Avg Score", value: fleetRpg.avgOverall }] : []),
  ];

  return (
    <div
      className="h-full w-full flex flex-col overflow-y-auto"
      style={{ display: visible ? "flex" : "none" }}
      data-testid="agents-view"
    >
      <div className="p-4 space-y-4">
        <MetricGrid metrics={metrics} />
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Sort:</span>
          {(["score", "health", "name"] as SortMode[]).map((mode) => (
            <button
              key={mode}
              onClick={() => setSortBy(mode)}
              className={`text-xs px-2 py-0.5 rounded transition-colors ${
                sortBy === mode
                  ? "bg-accent text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {mode.charAt(0).toUpperCase() + mode.slice(1)}
            </button>
          ))}
        </div>
        {isLoading && agents.length === 0 ? (
          <div className="flex items-center justify-center py-12 text-muted-foreground text-sm">
            Loading agents...
          </div>
        ) : (
          <AgentStatus agents={sortedAgents} summary={summary} />
        )}
        {/*
          Health above, manifests below: the top half is how the fleet is
          DOING (runs, scores, tiers, from the engine) and the bottom half is
          what the fleet IS (the manifests on disk, from the bridge). They are
          two different sources of truth about the same agents, and merging
          them into one table would make a manifest that will not load look
          like an agent that is merely unhealthy.
        */}
        <AgentManifests
          visible={visible}
          role={role}
          roleLoading={roleLoading}
          health={agents}
        />
      </div>
    </div>
  );
}
