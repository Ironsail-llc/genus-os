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
  healthy: { color: "text-success", bg: "bg-success/20", dotBg: "bg-success", border: "border-l-success", label: "Healthy" },
  degraded: { color: "text-warning", bg: "bg-warning/20", dotBg: "bg-warning", border: "border-l-warning", label: "Degraded" },
  failed: { color: "text-destructive", bg: "bg-destructive/20", dotBg: "bg-destructive", border: "border-l-destructive", label: "Failed" },
  sleeping: { color: "text-info", bg: "bg-info/20", dotBg: "bg-info", border: "border-l-info", label: "Sleeping" },
  unknown: { color: "text-muted-foreground", bg: "bg-muted", dotBg: "bg-muted-foreground", border: "border-l-muted-foreground", label: "Unknown" },
};

const scoreBarConfig: { key: keyof AgentRPG["scores"]; label: string; color: string }[] = [
  { key: "reliability", label: "REL", color: "bg-success" },
  { key: "debugging", label: "DBG", color: "bg-info" },
  { key: "patience", label: "PAT", color: "bg-primary" },
  { key: "wisdom", label: "WIS", color: "bg-warning" },
  { key: "chaos", label: "CHA", color: "bg-destructive" },
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
  if (score >= 70) return "text-success";
  if (score >= 40) return "text-warning";
  return "text-destructive";
}

function scoreBgColor(score: number): string {
  if (score >= 70) return "bg-success/20";
  if (score >= 40) return "bg-warning/20";
  return "bg-destructive/20";
}

function ScoreBars({ scores }: { scores: AgentRPG["scores"] }) {
  return (
    <div className="space-y-0.5 mt-1.5">
      {scoreBarConfig.map(({ key, label, color }) => {
        const value = scores[key];
        return (
          <div key={key} className="flex items-center gap-1.5">
            <span className="text-[9px] text-muted-foreground w-6 text-right font-mono">{label}</span>
            <div className="flex-1 h-1 rounded-full bg-muted overflow-hidden">
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
