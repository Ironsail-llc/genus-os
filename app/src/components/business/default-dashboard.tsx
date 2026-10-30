"use client";

import { ServiceHealth } from "./service-health";
import { SectionError } from "./section-error";
import { computeGreeting, formatToday, useIsClient } from "./greeting";
import { useHomeMetrics } from "./use-home-metrics";
import { useQuickActions } from "./quick-actions";

export function DefaultDashboard() {
  const {
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
  } = useHomeMetrics();
  const { actions: quickActions, failure: actionFailure, run: runQuickAction } = useQuickActions();
  const isClient = useIsClient();

  // Render only on the client; server emits empty strings so SSR/CSR match.
  const greeting = isClient ? computeGreeting() : "";
  const todayLabel = isClient ? formatToday() : "";

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
              {totalServices > 0 && troubled.length === 0
                ? `All services nominal${offCount > 0 ? ` · ${offCount} switched off` : ""}`
                : totalServices > 0
                  ? `${troubled
                      .map((s) => `${s.label ?? s.name} ${s.status === "unhealthy" ? "down" : s.status}`)
                      .join(", ")} — see service grid`
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
                onClick={() => runQuickAction(action)}
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
