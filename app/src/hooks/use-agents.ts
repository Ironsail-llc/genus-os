"use client";

import { useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAppQueryClient } from "@/lib/query/query-client";
import { executeTool, refusalOf } from "@/lib/query/fetch-refused";

type HealthTier = "healthy" | "degraded" | "failed" | "sleeping" | "unknown";

export interface AgentRPG {
  overall: number;
  level: number;
  levelName: string;
  totalXp: number;
  rank: number;
  scores: {
    debugging: number;
    patience: number;
    chaos: number;
    wisdom: number;
    reliability: number;
  };
  benchmarkScore?: number | null;
}

export interface AgentInfo {
  name: string;
  /**
   * The manifest id this health row belongs to (`agentId` in the bridge's
   * `agent_status`). The display `name` is an operator-renamable string; the id
   * is the only key this row and a manifest row can be joined on.
   */
  agentId?: string;
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

export interface AgentSummary {
  healthy: number;
  degraded: number;
  failed: number;
  sleeping: number;
  total: number;
}

const POLL_INTERVAL_MS = 60_000; // 60s polling, one beat for every reader
const STALE_MS = 60_000; // a reader mounting within a minute paints from cache

/** One cache entry for the fleet's health rows, shared by every reader. */
export const AGENTS_QUERY_KEY = ["agents", "status"] as const;

const EMPTY_SUMMARY: AgentSummary = { healthy: 0, degraded: 0, failed: 0, sleeping: 0, total: 0 };

async function fetchAgentStatus(signal: AbortSignal): Promise<AgentInfo[]> {
  const json = await executeTool("agent_status", {}, signal);
  return json.data?.agents || [];
}

function summarize(agentList: AgentInfo[]): AgentSummary {
  const s = { ...EMPTY_SUMMARY, total: agentList.length };
  for (const a of agentList) {
    if (a.status === "healthy") s.healthy++;
    else if (a.status === "degraded") s.degraded++;
    else if (a.status === "failed") s.failed++;
    else if (a.status === "sleeping") s.sleeping++;
  }
  return s;
}

export function useAgents() {
  const client = useAppQueryClient();
  const query = useQuery(
    {
      queryKey: AGENTS_QUERY_KEY,
      queryFn: ({ signal }) => fetchAgentStatus(signal),
      staleTime: STALE_MS,
      refetchInterval: POLL_INTERVAL_MS,
    },
    client
  );

  const agents = useMemo(() => query.data ?? [], [query.data]);
  const summary = useMemo(() => (query.data ? summarize(query.data) : EMPTY_SUMMARY), [query.data]);
  // A refused fetch must be reportable: "no agents" and "could not ask" are
  // not the same answer. Cleared by the next good answer.
  const error = refusalOf(query.error);
  const { refetch: refetchQuery } = query;
  const refetch = useCallback(async () => {
    await refetchQuery();
  }, [refetchQuery]);

  return { agents, summary, isLoading: query.isFetching || query.isPending, error, refetch };
}
