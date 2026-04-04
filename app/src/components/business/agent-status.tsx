"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import cronstrue from "cronstrue";
import type { AgentRPG } from "@/hooks/use-agents";

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
  rpg?: AgentRPG;
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

const scoreBarConfig: { key: keyof AgentRPG["scores"]; label: string; color: string }[] = [
  { key: "reliability", label: "REL", color: "bg-emerald-400" },
  { key: "debugging", label: "DBG", color: "bg-blue-400" },
  { key: "patience", label: "PAT", color: "bg-violet-400" },
  { key: "wisdom", label: "WIS", color: "bg-amber-400" },
  { key: "chaos", label: "CHA", color: "bg-red-400" },
];

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

function scoreColor(score: number): string {
  if (score >= 70) return "text-emerald-400";
  if (score >= 40) return "text-amber-400";
  return "text-red-400";
}

function scoreBgColor(score: number): string {
  if (score >= 70) return "bg-emerald-500/20";
  if (score >= 40) return "bg-amber-500/20";
  return "bg-red-500/20";
}

function ScoreBars({ scores }: { scores: AgentRPG["scores"] }) {
  return (
    <div className="space-y-0.5 mt-1.5">
      {scoreBarConfig.map(({ key, label, color }) => {
        const value = scores[key];
        return (
          <div key={key} className="flex items-center gap-1.5">
            <span className="text-[9px] text-muted-foreground w-6 text-right font-mono">{label}</span>
            <div className="flex-1 h-1 rounded-full bg-zinc-800 overflow-hidden">
              <div
                className={`h-full rounded-full ${color} transition-all`}
                style={{ width: `${value}%` }}
              />
            </div>
            <span className="text-[9px] text-muted-foreground w-5 text-right font-mono">{value}</span>
          </div>
        );
      })}
    </div>
  );
}

export function AgentStatus({ agents, summary }: AgentStatusProps) {
  return (
    <div data-testid="agent-status">
      {summary && (
        <div className="flex flex-wrap gap-3 mb-4" data-testid="agent-summary">
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
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {agents.map((agent) => {
          const tier = tierConfig[agent.status];
          const rpg = agent.rpg;
          return (
            <Card key={agent.name} className={`glass-panel border-l-2 ${tier.border}`} data-testid="agent-card">
              <CardHeader className="pb-1 pt-3 px-3">
                <div className="flex items-center gap-2">
                  <div
                    className={`w-2 h-2 rounded-full shrink-0 ${tier.dotBg}`}
                    data-testid="status-indicator"
                  />
                  <CardTitle className="text-sm flex-1">{agent.name}</CardTitle>
                  {rpg && (
                    <div className="flex items-center gap-1.5">
                      {rpg.rank > 0 && (
                        <span className="text-[10px] text-muted-foreground font-mono">#{rpg.rank}</span>
                      )}
                      <Badge className={`${scoreBgColor(rpg.overall)} ${scoreColor(rpg.overall)} text-[10px] px-1 py-0 font-mono`}>
                        {rpg.overall}
                      </Badge>
                    </div>
                  )}
                  {agent.errorCount ? (
                    <Badge variant="destructive" className="text-[10px] px-1 py-0">
                      {agent.errorCount} err
                    </Badge>
                  ) : null}
                </div>
                {rpg && (
                  <div className="flex items-center gap-1.5 mt-0.5 ml-4">
                    <span className="text-[10px] text-muted-foreground">
                      {rpg.levelName} Lv.{rpg.level}
                    </span>
                    <span className="text-[10px] text-muted-foreground">
                      {rpg.totalXp.toLocaleString()} XP
                    </span>
                  </div>
                )}
              </CardHeader>
              <CardContent className="px-3 pb-3 space-y-1">
                {rpg && <ScoreBars scores={rpg.scores} />}
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
