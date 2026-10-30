"use client";

import { useEffect, useState } from "react";
import { toSectionFailure, type SectionFailure } from "./section-error";
import { fetchHealth } from "@/lib/api/health";
import { useTasks } from "@/hooks/use-tasks";
import { useAgents } from "@/hooks/use-agents";
import type { HealthResponse } from "@/lib/api/types";

export const HEALTH_ENDPOINT = "/api/health";
const HEALTH_POLL_MS = 30000;

// Module-level stale-while-revalidate cache: the last good health snapshot
// paints instantly on a remount while a fresh probe runs, and survives a
// failed refresh — so a 401 shows an inline error *next to* the last known
// numbers instead of blanking the tile.
let healthCache: HealthResponse | null = null;

/**
 * The home figures the Dashboard view and the chat landing both show:
 * services healthy of total, active tasks, agents online. Read-only, no model
 * call. Health is polled every 30 seconds while a consumer is mounted.
 */
export function useHomeMetrics() {
  const [health, setHealth] = useState<HealthResponse | null>(healthCache);
  const [healthFailure, setHealthFailure] = useState<SectionFailure | null>(null);
  const { tasks, error: tasksError } = useTasks({ live: false });
  const { summary: agentSummary, error: agentsError } = useAgents();

  // A failed probe is reported, never swallowed into `null` — an empty card
  // and an unreachable service are not the same statement. State is only ever
  // set from the promise's callbacks, never synchronously in the effect body.
  useEffect(() => {
    const applyHealth = (next: HealthResponse) => {
      healthCache = next;
      setHealth(next);
      setHealthFailure(null);
    };
    const reportFailure = (err: unknown) => {
      setHealthFailure(toSectionFailure("System health", HEALTH_ENDPOINT, err));
    };

    fetchHealth().then(applyHealth).catch(reportFailure);
    const interval = setInterval(() => {
      fetchHealth().then(applyHealth).catch(reportFailure);
    }, HEALTH_POLL_MS);

    return () => clearInterval(interval);
  }, []);

  const activeTasks = tasks.filter(
    (t) => t.status === "TODO" || t.status === "IN_PROGRESS" || t.status === "REVIEW"
  ).length;

  // Defensive on the payload's shape, not just its presence: this feeds the
  // home screen, so a 200 whose body is missing `services` must degrade to
  // "awaiting first probe" rather than throw out of render and take the whole
  // app (chat panel included) down with it.
  const services = Array.isArray(health?.services) ? health.services : [];
  // A service the operator switched off is not counted: "2/2 · 1 switched off"
  // is the truth, "2/3" reads as an outage.
  const running = services.filter((s) => s.status !== "disabled");
  const healthyCount = running.filter((s) => s.status === "healthy").length;
  const totalServices = running.length;
  const offCount = services.length - running.length;
  const troubled = running.filter((s) => s.status !== "healthy");

  return {
    health,
    healthFailure,
    services,
    healthyCount,
    totalServices,
    offCount,
    troubled,
    activeTasks,
    tasksError,
    agentSummary,
    agentsError,
  };
}
