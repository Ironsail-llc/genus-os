"use client";

import { useEffect, useState } from "react";
import { ServiceHealth } from "./service-health";
import { fetchHealth } from "@/lib/api/health";
import { fetchPeople } from "@/lib/api/people";
import { fetchConversations } from "@/lib/api/conversations";
import { searchMemory } from "@/lib/api/memory";
import { useVisualState } from "@/hooks/use-visual-state";
import { useTasks } from "@/hooks/use-tasks";
import { useAgents } from "@/hooks/use-agents";
import { Users, Inbox, Brain, Activity } from "lucide-react";
import type { HealthResponse } from "@/lib/api/types";

function getGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

export function DefaultDashboard() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const { pushView } = useVisualState();
  const { tasks } = useTasks({ live: false });
  const { summary: agentSummary } = useAgents();

  useEffect(() => {
    fetchHealth()
      .then(setHealth)
      .catch(() => setHealth(null));

    const interval = setInterval(() => {
      fetchHealth()
        .then(setHealth)
        .catch(() => setHealth(null));
    }, 30000);

    return () => clearInterval(interval);
  }, []);

  const activeTasks = tasks.filter(
    (t) => t.status === "TODO" || t.status === "IN_PROGRESS" || t.status === "REVIEW"
  ).length;

  const healthyCount = health?.services.filter((s) => s.status === "healthy").length ?? 0;
  const totalServices = health?.services.length ?? 0;

  const quickActions = [
    {
      label: "Show my contacts",
      icon: Users,
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
      handler: async () => {
        const h = await fetchHealth();
        pushView({
          toolName: "render_service_health",
          props: { services: h.services, overallStatus: h.status },
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
          {getGreeting()}, Philip
        </h2>
        <span className="text-xs text-muted-foreground">
          {new Date().toLocaleDateString("en-US", {
            weekday: "long",
            year: "numeric",
            month: "long",
            day: "numeric",
          })}
        </span>
      </div>

      {/* Metric summary row */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3" data-testid="metric-summary">
        <div className="glass-panel p-4">
          <p className="text-xs text-muted-foreground mb-1">Active Tasks</p>
          <p className="text-2xl font-bold text-primary">{activeTasks}</p>
        </div>
        <div className="glass-panel p-4">
          <p className="text-xs text-muted-foreground mb-1">Agents Online</p>
          <p className="text-2xl font-bold text-emerald-400">{agentSummary.healthy}</p>
          {agentSummary.failed > 0 && (
            <p className="text-[10px] text-red-400">{agentSummary.failed} failed</p>
          )}
        </div>
        <div className="glass-panel p-4">
          <p className="text-xs text-muted-foreground mb-1">System Health</p>
          <p className="text-2xl font-bold text-emerald-400">
            {totalServices > 0 ? `${healthyCount}/${totalServices}` : "-"}
          </p>
        </div>
      </div>

      {/* Service health grid */}
      {health && (
        <ServiceHealth
          services={health.services}
          overallStatus={health.status}
        />
      )}

      {/* Quick actions */}
      <div className="glass-panel p-4">
        <h3 className="font-medium mb-3 text-sm">Quick Actions</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          {quickActions.map((action) => {
            const Icon = action.icon;
            return (
              <button
                key={action.label}
                onClick={() => action.handler().catch(console.error)}
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
