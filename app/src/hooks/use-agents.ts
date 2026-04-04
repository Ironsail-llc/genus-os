"use client";

import { useState, useEffect, useCallback, useRef } from "react";

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

const POLL_INTERVAL_MS = 60_000; // 60s polling
const STALE_THRESHOLD_MS = 60_000; // 60s tab-backgrounded -> refetch

export function useAgents() {
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [summary, setSummary] = useState<AgentSummary>({ healthy: 0, degraded: 0, failed: 0, sleeping: 0, total: 0 });
  const [isLoading, setIsLoading] = useState(true);
  const lastVisibleRef = useRef(Date.now());
  const fetchIdRef = useRef(0);

  const fetchAgents = useCallback(async () => {
    const id = ++fetchIdRef.current;
    try {
      setIsLoading(true);
      const res = await fetch("/api/actions/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tool: "agent_status", params: {} }),
      });
      if (!res.ok) return;
      const json = await res.json();
      if (id !== fetchIdRef.current) return;

      const agentList: AgentInfo[] = json.data?.agents || [];
      setAgents(agentList);

      // Compute summary
      const s = { healthy: 0, degraded: 0, failed: 0, sleeping: 0, total: agentList.length };
      for (const a of agentList) {
        if (a.status === "healthy") s.healthy++;
        else if (a.status === "degraded") s.degraded++;
        else if (a.status === "failed") s.failed++;
        else if (a.status === "sleeping") s.sleeping++;
      }
      setSummary(s);
    } finally {
      if (id === fetchIdRef.current) {
        setIsLoading(false);
      }
    }
  }, []);

  // Initial fetch
  useEffect(() => {
    fetchAgents();
  }, [fetchAgents]);

  // Polling
  useEffect(() => {
    const interval = setInterval(fetchAgents, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [fetchAgents]);

  // Page Visibility stale detection
  useEffect(() => {
    function onVisibilityChange() {
      if (document.visibilityState === "visible") {
        const elapsed = Date.now() - lastVisibleRef.current;
        if (elapsed > STALE_THRESHOLD_MS) {
          fetchAgents();
        }
      } else {
        lastVisibleRef.current = Date.now();
      }
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [fetchAgents]);

  return { agents, summary, isLoading, refetch: fetchAgents };
}
