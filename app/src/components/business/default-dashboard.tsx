"use client";

import { useEffect, useState } from "react";
import { ServiceHealth } from "./service-health";
import { fetchHealth } from "@/lib/api/health";
import { fetchPeople } from "@/lib/api/people";
import { fetchConversations } from "@/lib/api/conversations";
import { searchMemory } from "@/lib/api/memory";
import { useVisualState } from "@/hooks/use-visual-state";
import type { HealthResponse } from "@/lib/api/types";

export function DefaultDashboard() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const { pushView } = useVisualState();

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

  const quickActions = [
    {
      label: "Show my contacts",
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
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Robothor Dashboard</h2>
        <span className="text-xs text-muted-foreground">
          {new Date().toLocaleDateString("en-US", {
            weekday: "long",
            year: "numeric",
            month: "long",
            day: "numeric",
          })}
        </span>
      </div>

      {health && (
        <ServiceHealth
          services={health.services}
          overallStatus={health.status}
        />
      )}

      <div className="glass-panel p-4">
        <h3 className="font-medium mb-2 text-sm">Quick Actions</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          {quickActions.map((action) => (
            <button
              key={action.label}
              onClick={() => action.handler().catch(console.error)}
              className="text-xs text-left p-2 rounded-md bg-accent/50 hover:bg-accent transition-colors text-muted-foreground hover:text-foreground"
            >
              {action.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
