/**
 * Fetches context data for the welcome dashboard.
 * Server-side only — called from the welcome API route.
 */

import { getServiceUrl } from "@/lib/services/registry";
const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";
const ORCHESTRATOR_URL = getServiceUrl("orchestrator") || "http://localhost:9099";

interface WelcomeContext {
  timestamp: string;
  hour: number;
  dayOfWeek: string;
  greeting: string;
  health: {
    status: string;
    services: Array<{
      name: string;
      status: string;
      responseTime?: number;
    }>;
  } | null;
  inbox: {
    openCount: number;
    unreadCount: number;
  } | null;
  calendar: string | null;
  eventBus: {
    streams: Record<string, number>;
    total: number;
  } | null;
}

export async function fetchWelcomeContext(): Promise<WelcomeContext> {
  const now = new Date();
  const hour = now.getHours();
  const dayOfWeek = now.toLocaleDateString("en-US", { weekday: "long" });

  let greeting: string;
  if (hour >= 6 && hour < 12) greeting = "Good morning";
  else if (hour >= 12 && hour < 17) greeting = "Good afternoon";
  else if (hour >= 17 && hour < 22) greeting = "Good evening";
  else greeting = "Hey";

  // Fetch context in parallel — all are optional
  const [health, inbox, calendar, eventBus] = await Promise.all([
    fetchHealth(),
    fetchInbox(),
    fetchCalendar(),
    fetchEventBusStats(),
  ]);

  return {
    timestamp: now.toISOString(),
    hour,
    dayOfWeek,
    greeting: `${greeting}, Philip`,
    health,
    inbox,
    calendar,
    eventBus,
  };
}

async function fetchHealth() {
  try {
    const checks = await Promise.allSettled([
      fetchJson(`${BRIDGE_URL}/health`),
      fetchJson(`${ORCHESTRATOR_URL}/health`),
      fetchJson(`${getServiceUrl("vision") || "http://localhost:8600"}/health`),
    ]);
    const names = ["bridge", "orchestrator", "vision"];
    const services = checks.map((c, i) => ({
      name: names[i],
      status: c.status === "fulfilled" ? "healthy" : "unhealthy",
      responseTime: c.status === "fulfilled" ? c.value?.responseTime : undefined,
    }));
    const allHealthy = services.every((s) => s.status === "healthy");
    return {
      status: allHealthy ? "ok" : "degraded",
      services,
    };
  } catch {
    return null;
  }
}

async function fetchInbox() {
  try {
    const data = await fetchJson(
      `${BRIDGE_URL}/api/conversations?status=open`
    );
    const conversations = data?.data?.payload ?? [];
    const unreadCount = conversations.reduce(
      (sum: number, c: { unread_count?: number }) =>
        sum + (c.unread_count || 0),
      0
    );
    return {
      openCount: conversations.length,
      unreadCount,
    };
  } catch {
    return null;
  }
}

async function fetchCalendar() {
  try {
    const data = await fetchJson(
      `${ORCHESTRATOR_URL}/query`,
      {
        method: "POST",
        body: JSON.stringify({
          question: "What meetings or events are scheduled for today?",
          limit: 3,
        }),
      },
      3000 // tight timeout — calendar is optional
    );
    return data?.answer || null;
  } catch {
    return null;
  }
}

async function fetchEventBusStats() {
  try {
    const { streamLengths } = await import("@/lib/event-bus/redis-client");
    const streams = await streamLengths();
    const total = Object.values(streams).reduce((sum, n) => sum + n, 0);
    return { streams, total };
  } catch {
    return null;
  }
}

async function fetchJson(url: string, options?: RequestInit, timeoutMs = 5000) {
  const res = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
    signal: AbortSignal.timeout(timeoutMs),
  });
  return res.json();
}
