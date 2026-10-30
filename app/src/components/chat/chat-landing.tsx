"use client";

import type { ReactNode } from "react";
import { SectionError } from "@/components/business/section-error";
import { computeGreeting, formatToday, useIsClient } from "@/components/business/greeting";
import { useHomeMetrics } from "@/components/business/use-home-metrics";
import { useQuickActions } from "@/components/business/quick-actions";

interface ChatLandingProps {
  /** The owner's first name, when the session has one. */
  ownerName?: string;
  suggestedPrompts: string[];
  /** A chip hands its text to the composer. */
  onPrompt: (prompt: string) => void;
}

function MetricTile({ label, testId, children }: { label: string; testId: string; children: ReactNode }) {
  return (
    <div className="glass-panel px-3 py-2.5" data-testid={testId}>
      <p className="text-[11px] font-medium uppercase tracking-[0.07em] text-muted-foreground/80">
        {label}
      </p>
      {children}
    </div>
  );
}

/**
 * What the chat home shows before the first message: a greeting, the
 * Dashboard view's three figures, its quick actions and the suggested
 * prompts. Read-only, no model call; the composer stays below it.
 */
export function ChatLanding({ ownerName, suggestedPrompts, onPrompt }: ChatLandingProps) {
  const {
    healthFailure,
    healthyCount,
    totalServices,
    activeTasks,
    tasksError,
    agentSummary,
    agentsError,
  } = useHomeMetrics();
  const { actions, failure: actionFailure, run } = useQuickActions();
  const isClient = useIsClient();

  // Render only on the client; server emits empty strings so SSR/CSR match.
  const greeting = isClient ? computeGreeting() : "";
  const todayLabel = isClient ? formatToday() : "";
  const name = ownerName?.trim();

  return (
    <div className="space-y-6 py-4 md:py-8" data-testid="chat-landing">
      <div>
        <h2 className="text-2xl font-semibold" data-testid="chat-landing-greeting">
          {greeting ? (name ? `${greeting}, ${name}` : greeting) : " "}
        </h2>
        <span className="text-xs text-muted-foreground">{todayLabel || " "}</span>
      </div>

      <div className="grid grid-cols-3 gap-2" data-testid="chat-landing-metrics">
        <MetricTile label="Services" testId="landing-metric-services">
          {healthFailure ? (
            <SectionError failure={healthFailure} testId="section-error-health" className="mt-1" />
          ) : (
            <p
              className={`font-mono text-xl font-semibold tabular-nums ${
                totalServices > 0 && healthyCount < totalServices ? "text-warning" : "text-foreground"
              }`}
            >
              {totalServices > 0 ? `${healthyCount}/${totalServices}` : "-"}
            </p>
          )}
        </MetricTile>
        <MetricTile label="Active tasks" testId="landing-metric-tasks">
          {tasksError ? (
            <SectionError
              failure={{ label: "Tasks", endpoint: tasksError.endpoint, status: tasksError.status }}
              testId="section-error-tasks"
              className="mt-1"
            />
          ) : (
            <p className="font-mono text-xl font-semibold tabular-nums text-foreground">{activeTasks}</p>
          )}
        </MetricTile>
        <MetricTile label="Agents online" testId="landing-metric-agents">
          {agentsError ? (
            <SectionError
              failure={{ label: "Agents", endpoint: agentsError.endpoint, status: agentsError.status }}
              testId="section-error-agents"
              className="mt-1"
            />
          ) : (
            <p className="font-mono text-xl font-semibold tabular-nums text-foreground">
              {agentSummary.healthy}
            </p>
          )}
        </MetricTile>
      </div>

      <div>
        {actionFailure && (
          <SectionError failure={actionFailure} testId="section-error-actions" className="mb-2" />
        )}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          {actions.map((action) => {
            const Icon = action.icon;
            return (
              <button
                key={action.label}
                onClick={() => run(action)}
                className="flex items-center gap-2 rounded-lg bg-accent/50 px-3 py-2.5 text-left text-muted-foreground transition-colors hover:bg-accent hover:text-foreground min-h-[44px]"
                data-testid="quick-action"
              >
                <Icon className="w-4 h-4 shrink-0" />
                <span className="text-xs">{action.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {suggestedPrompts.map((prompt) => (
          <button
            key={prompt}
            onClick={() => onPrompt(prompt)}
            className="text-xs px-3 py-2 md:py-1.5 rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-accent transition-colors min-h-[44px] md:min-h-0"
            data-testid="suggested-prompt"
          >
            {prompt}
          </button>
        ))}
      </div>
    </div>
  );
}
