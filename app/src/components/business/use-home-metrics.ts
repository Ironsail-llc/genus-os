"use client";

import { useSyncExternalStore } from "react";
import { toSectionFailure, type SectionFailure } from "./section-error";
import { fetchHealth } from "@/lib/api/health";
import { useTasks } from "@/hooks/use-tasks";
import { useAgents } from "@/hooks/use-agents";
import type { HealthResponse } from "@/lib/api/types";

export const HEALTH_ENDPOINT = "/api/health";
const HEALTH_POLL_MS = 30000;

// One health poll for every consumer. The Dashboard view stays mounted while
// hidden, so on the chat home it and the landing both read health; a module
// store with one interval keeps them on the same figure and the same request.
//
// The last good snapshot survives a remount and a failed refresh (stale-while-
// revalidate), so a 401 shows an inline error *next to* the last known numbers
// instead of blanking the tile. The failure is cleared when the last consumer
// goes, so a later mount reports only what its own probes find.
interface HealthState {
  health: HealthResponse | null;
  failure: SectionFailure | null;
}

let healthState: HealthState = { health: null, failure: null };
const healthListeners = new Set<() => void>();
let healthInterval: ReturnType<typeof setInterval> | null = null;

function setHealthState(next: HealthState) {
  healthState = next;
  healthListeners.forEach((listener) => listener());
}

function probeHealth() {
  fetchHealth()
    .then((next) => setHealthState({ health: next, failure: null }))
    .catch((err: unknown) =>
      setHealthState({
        health: healthState.health,
        failure: toSectionFailure("System health", HEALTH_ENDPOINT, err),
      }),
    );
}

function subscribeHealth(listener: () => void) {
  healthListeners.add(listener);
  if (healthListeners.size === 1) {
    probeHealth();
    healthInterval = setInterval(probeHealth, HEALTH_POLL_MS);
  }
  return () => {
    healthListeners.delete(listener);
    if (healthListeners.size === 0) {
      if (healthInterval) clearInterval(healthInterval);
      healthInterval = null;
      healthState = { health: healthState.health, failure: null };
    }
  };
}

const getHealthState = () => healthState;
const SERVER_HEALTH_STATE: HealthState = { health: null, failure: null };
const getServerHealthState = () => SERVER_HEALTH_STATE;

/**
 * The home figures the Dashboard view and the chat landing both show:
 * services healthy of total, active tasks, agents online. Read-only, no model
 * call. Health is polled every 30 seconds while any consumer is mounted.
 */
export function useHomeMetrics() {
  const { health, failure: healthFailure } = useSyncExternalStore(
    subscribeHealth,
    getHealthState,
    getServerHealthState,
  );
  const { tasks, error: tasksError } = useTasks({ live: false });
  const { summary: agentSummary, error: agentsError } = useAgents();

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
