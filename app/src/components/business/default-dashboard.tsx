"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { ServiceHealth } from "./service-health";
import { SectionError, toSectionFailure, type SectionFailure } from "./section-error";
import { fetchHealth } from "@/lib/api/health";
import { fetchPeople } from "@/lib/api/people";
import { fetchConversations } from "@/lib/api/conversations";
import { searchMemory } from "@/lib/api/memory";
import { useVisualState } from "@/hooks/use-visual-state";
import { useTasks } from "@/hooks/use-tasks";
import { useAgents } from "@/hooks/use-agents";
import { Users, Inbox, Brain, Activity } from "lucide-react";
import type { HealthResponse } from "@/lib/api/types";

function computeGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function formatToday(): string {
  return new Date().toLocaleDateString("en-US", {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

// `useSyncExternalStore` with constant snapshots is the React-19-blessed way
// to render different output on server vs client without a hydration mismatch
// and without a setState-in-useEffect that trips react-hooks linting and
// pollutes test runs. The server snapshot is `false`, the client snapshot is
// `true`, and the subscribe fn is a no-op (the value never changes after
// mount).
const subscribe = () => () => {};
function useIsClient(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => true,
    () => false,
  );
}

const HEALTH_ENDPOINT = "/api/health";

// Module-level stale-while-revalidate cache: the last good health snapshot
// paints instantly on a remount while a fresh probe runs, and survives a
// failed refresh — so a 401 shows an inline error *next to* the last known
// numbers instead of blanking the tile.
let healthCache: HealthResponse | null = null;

export function DefaultDashboard() {
  const [health, setHealth] = useState<HealthResponse | null>(healthCache);
  const [healthFailure, setHealthFailure] = useState<SectionFailure | null>(null);
  const [actionFailure, setActionFailure] = useState<SectionFailure | null>(null);
  const { pushView } = useVisualState();
  const { tasks, error: tasksError } = useTasks({ live: false });
  const { summary: agentSummary, error: agentsError } = useAgents();
  const isClient = useIsClient();

  // Render only on the client; server emits empty strings so SSR/CSR match.
  const greeting = isClient ? computeGreeting() : "";
  const todayLabel = isClient ? formatToday() : "";

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
    }, 30000);

    return () => clearInterval(interval);
  }, []);

  const activeTasks = tasks.filter(
    (t) => t.status === "TODO" || t.status === "IN_PROGRESS" || t.status === "REVIEW"
  ).length;

  // Defensive on the payload's shape, not just its presence: this component is
  // the home screen, so a 200 whose body is missing `services` must degrade to
  // "awaiting first probe" rather than throw out of render and take the whole
  // app (chat panel included) down with it.
  const services = Array.isArray(health?.services) ? health.services : [];
  const healthyCount = services.filter((s) => s.status === "healthy").length;
  const totalServices = services.length;

  const quickActions = [
    {
      label: "Show my contacts",
      icon: Users,
      failureLabel: "Contacts",
      endpoint: "/api/bridge/api/people",
      handler: async () => {
        const people = await fetchPeople();
        pushView({
          toolName: "render_contact_table",
          props: { data: people },
          title: "All Contacts",
        });
      },
    },
    {
      label: "Check inbox",
      icon: Inbox,
      failureLabel: "Conversations",
      endpoint: "/api/bridge/api/conversations",
      handler: async () => {
        const conversations = await fetchConversations();
        pushView({
          toolName: "render_conversations",
          props: { conversations },
          title: "Conversations",
        });
      },
    },
    {
      label: "Search memory",
      icon: Brain,
      failureLabel: "Memory",
      endpoint: "/api/orchestrator/query",
      handler: async () => {
        const results = await searchMemory("recent");
        pushView({
          toolName: "render_memory_search",
          props: { results, query: "recent" },
          title: 'Memory: "recent"',
        });
      },
    },
    {
      label: "Service health",
      icon: Activity,
      failureLabel: "System health",
      endpoint: HEALTH_ENDPOINT,
      handler: async () => {
        const h = await fetchHealth();
        pushView({
          toolName: "render_service_health",
          props: { services: h?.services ?? [], overallStatus: h?.status },
          title: "Service Health",
        });
      },
    },
  ];

  return (
    <div className="space-y-6" data-testid="default-dashboard">
      {/* Greeting */}
      <div>
        <h2 className="text-xl font-semibold" data-testid="greeting">
          {greeting || " "}
        </h2>
        <span className="text-xs text-muted-foreground">
          {todayLabel || " "}
        </span>
      </div>

      {/* Metric summary — bento grid; color appears only when a threshold is
          crossed, so a calm all-gray board reads as "everything is fine". */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3" data-testid="metric-summary">
        <div className="glass-panel col-span-2 p-4">
          <p className="text-[11px] font-medium uppercase tracking-[0.07em] text-muted-foreground/80 mb-1">
            System Health
          </p>
          <p
            className={`font-mono text-3xl font-semibold tabular-nums ${
              totalServices > 0 && healthyCount < totalServices ? "text-warning" : "text-foreground"
            }`}
          >
            {totalServices > 0 ? (
              <>
                {healthyCount}
                <span className="text-lg text-muted-foreground/60">/{totalServices}</span>
              </>
            ) : (
              "-"
            )}
          </p>
          {healthFailure ? (
            <SectionError
              failure={healthFailure}
              testId="section-error-health"
              className="mt-2"
            />
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">
              {totalServices > 0 && healthyCount === totalServices
                ? "All services nominal"
                : totalServices > 0
                  ? "Degraded — see service grid"
                  : "Awaiting first probe"}
            </p>
          )}
        </div>
        <div className="glass-panel p-4">
          <p className="text-[11px] font-medium uppercase tracking-[0.07em] text-muted-foreground/80 mb-1">
            Active Tasks
          </p>
          {tasksError ? (
            <SectionError
              failure={{ label: "Tasks", endpoint: tasksError.endpoint, status: tasksError.status }}
              testId="section-error-tasks"
            />
          ) : (
            <p className="font-mono text-2xl font-semibold tabular-nums text-foreground">{activeTasks}</p>
          )}
        </div>
        <div className="glass-panel p-4">
          <p className="text-[11px] font-medium uppercase tracking-[0.07em] text-muted-foreground/80 mb-1">
            Agents Online
          </p>
          {agentsError ? (
            <SectionError
              failure={{ label: "Agents", endpoint: agentsError.endpoint, status: agentsError.status }}
              testId="section-error-agents"
            />
          ) : (
            <>
              <p className="font-mono text-2xl font-semibold tabular-nums text-foreground">{agentSummary.healthy}</p>
              {agentSummary.failed > 0 && (
                <p className="text-[10px] text-destructive">{agentSummary.failed} failed</p>
              )}
            </>
          )}
        </div>
      </div>

      {/* Service health grid — the last good snapshot stays on screen while a
          refresh fails, with the failure reported in the tile above. */}
      {services.length > 0 && (
        <ServiceHealth services={services} overallStatus={health?.status} />
      )}

      {/* Quick actions */}
      <div className="glass-panel p-4">
        <h3 className="font-medium mb-3 text-sm">Quick Actions</h3>
        {actionFailure && (
          <SectionError
            failure={actionFailure}
            testId="section-error-actions"
            className="mb-3"
          />
        )}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          {quickActions.map((action) => {
            const Icon = action.icon;
            return (
              <button
                key={action.label}
                onClick={() => {
                  setActionFailure(null);
                  action.handler().catch((err) => {
                    setActionFailure(
                      toSectionFailure(action.failureLabel, action.endpoint, err),
                    );
                  });
                }}
                className="flex flex-col items-center gap-2 p-4 rounded-lg bg-accent/50 hover:bg-accent transition-colors text-muted-foreground hover:text-foreground"
                data-testid="quick-action"
              >
                <Icon className="w-5 h-5" />
                <span className="text-xs text-center">{action.label}</span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
