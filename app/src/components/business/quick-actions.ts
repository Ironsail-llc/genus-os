"use client";

import { useState } from "react";
import { Users, Inbox, Brain, Activity, type LucideIcon } from "lucide-react";
import { toSectionFailure, type SectionFailure } from "./section-error";
import { fetchHealth } from "@/lib/api/health";
import { fetchPeople } from "@/lib/api/people";
import { fetchConversations } from "@/lib/api/conversations";
import { searchMemory } from "@/lib/api/memory";
import { useVisualState } from "@/hooks/use-visual-state";
import { QUICK_ACTION_LABELS } from "./quick-action-labels";

export interface QuickAction {
  label: string;
  icon: LucideIcon;
  /** The section named in the failure notice, e.g. "Contacts". */
  failureLabel: string;
  endpoint: string;
  handler: () => Promise<void>;
}

type PushView = ReturnType<typeof useVisualState>["pushView"];

/** The home quick actions: each reads one endpoint and opens a canvas view. */
export function buildQuickActions(pushView: PushView): QuickAction[] {
  return [
    {
      label: QUICK_ACTION_LABELS.contacts,
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
      label: QUICK_ACTION_LABELS.inbox,
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
      label: QUICK_ACTION_LABELS.memory,
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
      label: QUICK_ACTION_LABELS.health,
      icon: Activity,
      failureLabel: "System health",
      endpoint: "/api/health",
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
}

/**
 * The quick actions with their run handler: a failed action is reported
 * (endpoint and status), never swallowed into the console.
 */
export function useQuickActions() {
  const { pushView } = useVisualState();
  const [failure, setFailure] = useState<SectionFailure | null>(null);
  const actions = buildQuickActions(pushView);

  const run = (action: QuickAction) => {
    setFailure(null);
    action.handler().catch((err) => {
      setFailure(toSectionFailure(action.failureLabel, action.endpoint, err));
    });
  };

  return { actions, failure, run };
}
