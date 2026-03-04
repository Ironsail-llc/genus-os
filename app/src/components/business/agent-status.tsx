"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import cronstrue from "cronstrue";

type HealthTier = "healthy" | "degraded" | "failed" | "sleeping" | "unknown";

interface AgentInfo {
  name: string;
  schedule: string;
  scheduleHuman?: string;
  lastRun?: string;
  lastDuration?: number;
  nextRun?: string;
  status: HealthTier;
  statusSummary?: string;
  errorCount?: number;
  pendingTasks?: number;
  enabled?: boolean;
}

interface AgentStatusProps {
  agents: AgentInfo[];
  summary?: { healthy: number; degraded: number; failed: number; sleeping: number; total: number };
}

const tierConfig: Record<HealthTier, { color: string; bg: string; dotBg: string; border: string; label: string }> = {
  healthy: { color: "text-emerald-400", bg: "bg-emerald-500/20", dotBg: "bg-emerald-400", border: "border-l-emerald-400", label: "Healthy" },
  degraded: { color: "text-amber-400", bg: "bg-amber-500/20", dotBg: "bg-amber-400", border: "border-l-amber-400", label: "Degraded" },
  failed: { color: "text-red-400", bg: "bg-red-500/20", dotBg: "bg-red-400", border: "border-l-red-400", label: "Failed" },
  sleeping: { color: "text-blue-400", bg: "bg-blue-500/20", dotBg: "bg-blue-400", border: "border-l-blue-400", label: "Sleeping" },
  unknown: { color: "text-zinc-500", bg: "bg-zinc-700/20", dotBg: "bg-zinc-500", border: "border-l-zinc-500", label: "Unknown" },
};

function formatDuration(ms?: number): string {
  if (ms == null) return "-";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatRelativeTime(iso?: string): string {
  if (!iso) return "-";
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

function humanCron(expr: string): string {
  try {
    return cronstrue.toString(expr, { use24HourTimeFormat: true });
  } catch {
    return expr;
  }
}

export function AgentStatus({ agents, summary }: AgentStatusProps) {
  return (
    <div data-testid="agent-status">
      {summary && (
        <div className="flex gap-3 mb-4" data-testid="agent-summary">
          <Badge className={tierConfig.healthy.bg + " " + tierConfig.healthy.color}>
            {summary.healthy} healthy
          </Badge>
          <Badge className={tierConfig.degraded.bg + " " + tierConfig.degraded.color}>
            {summary.degraded} degraded
          </Badge>
          <Badge className={tierConfig.failed.bg + " " + tierConfig.failed.color}>
            {summary.failed} failed
          </Badge>
          <Badge className={tierConfig.sleeping.bg + " " + tierConfig.sleeping.color}>
            {summary.sleeping} sleeping
          </Badge>
        </div>
      )}
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
        {agents.map((agent) => {
          const tier = tierConfig[agent.status];
          return (
            <Card key={agent.name} className={`glass-panel border-l-2 ${tier.border}`} data-testid="agent-card">
              <CardHeader className="pb-1 pt-3 px-3">
                <div className="flex items-center gap-2">
                  <div
                    className={`w-2 h-2 rounded-full shrink-0 ${tier.dotBg}`}
                    data-testid="status-indicator"
                  />
                  <CardTitle className="text-sm flex-1">{agent.name}</CardTitle>
                  {agent.errorCount ? (
                    <Badge variant="destructive" className="text-[10px] px-1 py-0">
                      {agent.errorCount} err
                    </Badge>
                  ) : null}
                </div>
              </CardHeader>
              <CardContent className="px-3 pb-3 space-y-1">
                <p className="text-xs text-muted-foreground">
                  {agent.scheduleHuman || humanCron(agent.schedule)}
                </p>
                <div className="flex justify-between text-xs text-muted-foreground">
                  <span>Last: {formatRelativeTime(agent.lastRun)}</span>
                  <span>{formatDuration(agent.lastDuration)}</span>
                </div>
                {agent.statusSummary && (
                  <p className="text-xs text-muted-foreground line-clamp-2">
                    {agent.statusSummary}
                  </p>
                )}
                {(agent.pendingTasks ?? 0) > 0 && (
                  <Badge variant="outline" className="text-[10px] px-1 py-0">
                    {agent.pendingTasks} pending
                  </Badge>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
