/**
 * Fetches context data for the welcome dashboard.
 * Server-side only — called from the welcome API route.
 */

import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { getServiceUrl } from "@/lib/services/registry";
import { OWNER_NAME } from "@/lib/config";
const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";
const ORCHESTRATOR_URL = getServiceUrl("orchestrator") || "http://localhost:9099";
const VISION_URL = getServiceUrl("vision") || "http://localhost:8600";

// The operator's credential goes only to backends that authenticate it —
// the same gate `conversation-context.ts` applies, so a URL that resolves
// anywhere else can never carry the token.
const AUTHENTICATED_ORIGINS = new Set(
  [BRIDGE_URL, ORCHESTRATOR_URL, VISION_URL].map((u) => new URL(u).origin)
);

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

  // These run inside a route handler on behalf of the signed-in operator:
  // resolve that caller's bridge credential once and send it with every
  // backend call. Without it the bridge answers 401 and the welcome dashboard
  // is generated from nothing.
  const authHeaders = await bridgeAuthHeaders();

  // Fetch context in parallel — all are optional
  const [health, inbox, calendar, eventBus] = await Promise.all([
    fetchHealth(authHeaders),
    fetchInbox(authHeaders),
    fetchCalendar(authHeaders),
    fetchEventBusStats(),
  ]);

  return {
    timestamp: now.toISOString(),
    hour,
    dayOfWeek,
    greeting: `${greeting}, ${OWNER_NAME}`,
    health,
    inbox,
    calendar,
    eventBus,
  };
}

async function fetchHealth(authHeaders: Record<string, string>) {
  try {
    const checks = await Promise.allSettled([
      fetchJson(authHeaders, `${BRIDGE_URL}/health`),
      fetchJson(authHeaders, `${ORCHESTRATOR_URL}/health`),
      fetchJson(authHeaders, `${VISION_URL}/health`),
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

async function fetchInbox(authHeaders: Record<string, string>) {
  try {
    const data = await fetchJson(
      authHeaders,
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

async function fetchCalendar(authHeaders: Record<string, string>) {
  try {
    const data = await fetchJson(
      authHeaders,
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

/**
 * Resolve `work`, or give up after `timeoutMs`.
 *
 * Every HTTP source here is bounded by an AbortSignal; this bounds the one
 * that is not. The timer is always cleared, so a fast answer does not leave a
 * pending handle behind.
 */
function withTimeout<T>(work: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expiry = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out`)), timeoutMs);
  });
  return Promise.race([work, expiry]).finally(() => clearTimeout(timer)) as Promise<T>;
}

// The event bus is a Redis read, and an unreachable Redis does not fail — its
// client retries with backoff. Unbounded, that held the welcome route open
// behind its keepalive for a section the dashboard is happy to skip.
const EVENT_BUS_TIMEOUT_MS = 2000;

async function fetchEventBusStats() {
  try {
    const { streamLengths } = await import("@/lib/event-bus/redis-client");
    const streams = await withTimeout(streamLengths(), EVENT_BUS_TIMEOUT_MS, "event bus");
    const total = Object.values(streams).reduce((sum, n) => sum + n, 0);
    return { streams, total };
  } catch {
    return null;
  }
}

async function fetchJson(
  authHeaders: Record<string, string>,
  url: string,
  options?: RequestInit,
  timeoutMs = 5000,
) {
  // Resolve the request against its own configured backend origin and assert
  // the resolved origin is unchanged — this breaks SSRF taint flows where a
  // path segment could escape the intended backend.
  const base = new URL(url);
  const target = new URL(
    base.pathname.replace(/^\/+/, "") + base.search,
    base.origin + "/"
  );
  if (target.origin !== base.origin) {
    throw new Error("Bad gateway path");
  }
  const credentials = AUTHENTICATED_ORIGINS.has(target.origin) ? authHeaders : {};
  const res = await fetch(target.toString(), {
    ...options,
    headers: { "Content-Type": "application/json", ...credentials, ...options?.headers },
    signal: AbortSignal.timeout(timeoutMs),
  });
  return res.json();
}
