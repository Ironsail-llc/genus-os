/**
 * Topic-aware parallel data fetching for conversation-driven dashboards.
 * Server-side only — called from the dashboard generate API route.
 */

import { createHash } from "crypto";
import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { getServiceUrl } from "@/lib/services/registry";
const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";
const ORCHESTRATOR_URL = getServiceUrl("orchestrator") || "http://localhost:9099";
const VISION_URL = getServiceUrl("vision") || "http://localhost:8600";
const SEARXNG_URL = getServiceUrl("searxng") || "http://localhost:8888";
const FETCH_TIMEOUT = 5000;

// Trusted backend origins — every outbound fetch must resolve to one of these.
// Guards against SSRF if a config/user value ever escapes the intended backend.
const TRUSTED_ORIGINS = new Set(
  [BRIDGE_URL, ORCHESTRATOR_URL, VISION_URL, SEARXNG_URL].map(
    (u) => new URL(u).origin
  )
);

// Origins that authenticate their caller. SearXNG is an unauthenticated
// internal index, so the operator's bearer token is never sent there.
const AUTHENTICATED_ORIGINS = new Set(
  [BRIDGE_URL, ORCHESTRATOR_URL, VISION_URL].map((u) => new URL(u).origin)
);

/**
 * The signed-in operator on whose behalf these server-side fetches run.
 *
 * Resolved once per request and threaded through, so one dashboard build costs
 * one session lookup rather than one per data need. Without the bearer the
 * bridge answers 401 and the model is handed an empty dashboard.
 */
export interface BackendCaller {
  headers: Record<string, string>;
  /** Cache partition — derived from, never equal to, the credential. */
  scope: string;
}

async function resolveCaller(): Promise<BackendCaller> {
  const headers = await bridgeAuthHeaders();
  const credential = headers.Authorization ?? headers["X-Agent-Id"] ?? "";
  const scope = credential
    ? createHash("sha256").update(credential).digest("hex").slice(0, 16)
    : "anonymous";
  return { headers, scope };
}

// ── TTL Cache ──────────────────────────────────────────────────
interface CacheEntry { data: Record<string, unknown>; expiresAt: number }
const dataCache = new Map<string, CacheEntry>();
const CACHE_TTL: Record<string, number> = {
  contacts:      5 * 60_000,   // 5 min
  companies:     5 * 60_000,
  health:        30_000,       // 30s
  conversations: 60_000,       // 1 min
  calendar:      60 * 60_000,  // 1 hr
  overview:      30_000,
};

/** Clear all cached data — exposed for testing. */
export function clearDataCache() { dataCache.clear(); }

// Cached rows are partitioned per caller: they were fetched with one
// operator's credential and must never be replayed into another's dashboard.
function cacheKey(caller: BackendCaller, key: string): string {
  return `${caller.scope}:${key}`;
}

function getCached(caller: BackendCaller, key: string): Record<string, unknown> | null {
  const entry = dataCache.get(cacheKey(caller, key));
  if (!entry) return null;
  if (Date.now() > entry.expiresAt) { dataCache.delete(cacheKey(caller, key)); return null; }
  return entry.data;
}

function setCache(caller: BackendCaller, key: string, data: Record<string, unknown>) {
  const ttl = CACHE_TTL[key] ?? 60_000;
  dataCache.set(cacheKey(caller, key), { data, expiresAt: Date.now() + ttl });
}

export interface ConversationContext {
  topic: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export async function fetchConversationContext(
  topic: string,
  searchQuery?: string
): Promise<ConversationContext> {
  let data: Record<string, unknown> = {};
  const caller = await resolveCaller();

  try {
    switch (topic) {
      case "contacts":
        data = await fetchContacts(caller);
        break;
      case "inbox":
        data = await fetchInbox(caller);
        break;
      case "health":
        data = await fetchHealth(caller);
        break;
      case "memory":
        data = await fetchMemory(caller, searchQuery || "recent information");
        break;
      case "companies":
        data = await fetchCompanies(caller);
        break;
      case "calendar":
        data = await fetchCalendar(caller);
        break;
      case "overview":
        data = await fetchOverview(caller);
        break;
      case "general":
        // No pre-fetched data — Gemini renders from conversation alone
        data = {};
        break;
      default:
        data = {};
    }
  } catch {
    // Graceful degradation — return empty data
    data = { error: "Failed to fetch context data" };
  }

  return {
    topic,
    data,
    timestamp: new Date().toISOString(),
  };
}

async function fetchContacts(caller: BackendCaller): Promise<Record<string, unknown>> {
  const cached = getCached(caller, "contacts");
  if (cached) return cached;
  try {
    const res = await fetchJson(caller, `${BRIDGE_URL}/api/people?limit=20`);
    const data = { people: res?.data || [] };
    setCache(caller, "contacts", data);
    return data;
  } catch {
    return { people: [] };
  }
}

async function fetchInbox(caller: BackendCaller): Promise<Record<string, unknown>> {
  const cached = getCached(caller, "conversations");
  if (cached) return cached;
  try {
    const res = await fetchJson(caller, `${BRIDGE_URL}/api/conversations?status=open`);
    const conversations = res?.data?.payload ?? [];
    const unreadCount = conversations.reduce(
      (sum: number, c: { unread_count?: number }) => sum + (c.unread_count || 0),
      0
    );
    const data = {
      conversations,
      openCount: conversations.length,
      unreadCount,
    };
    setCache(caller, "conversations", data);
    return data;
  } catch {
    return { conversations: [], openCount: 0, unreadCount: 0 };
  }
}

async function fetchHealth(caller: BackendCaller): Promise<Record<string, unknown>> {
  const cached = getCached(caller, "health");
  if (cached) return cached;
  const checks = await Promise.allSettled([
    fetchJson(caller, `${BRIDGE_URL}/health`),
    fetchJson(caller, `${ORCHESTRATOR_URL}/health`),
    fetchJson(caller, `${VISION_URL}/health`),
  ]);
  const names = ["bridge", "orchestrator", "vision"];
  const services = checks.map((c, i) => ({
    name: names[i],
    status: c.status === "fulfilled" ? "healthy" : "unhealthy",
  }));
  const allHealthy = services.every((s) => s.status === "healthy");
  const data = {
    status: allHealthy ? "ok" : "degraded",
    services,
  };
  setCache(caller, "health", data);
  return data;
}

async function fetchMemory(caller: BackendCaller, query: string): Promise<Record<string, unknown>> {
  try {
    const res = await fetchJson(caller, `${ORCHESTRATOR_URL}/query`, {
      method: "POST",
      body: JSON.stringify({ question: query, limit: 5 }),
    });
    return { answer: res?.answer || null, query };
  } catch {
    return { answer: null, query };
  }
}

async function fetchCompanies(caller: BackendCaller): Promise<Record<string, unknown>> {
  const cached = getCached(caller, "companies");
  if (cached) return cached;
  try {
    const [people, companies] = await Promise.allSettled([
      fetchJson(caller, `${BRIDGE_URL}/api/people?limit=20`),
      fetchJson(caller, `${BRIDGE_URL}/api/companies?limit=20`),
    ]);
    const data = {
      people: people.status === "fulfilled" ? people.value?.data || [] : [],
      companies: companies.status === "fulfilled" ? companies.value?.data || [] : [],
    };
    setCache(caller, "companies", data);
    return data;
  } catch {
    return { people: [], companies: [] };
  }
}

async function fetchCalendar(caller: BackendCaller): Promise<Record<string, unknown>> {
  const cached = getCached(caller, "calendar");
  if (cached) return cached;
  try {
    const res = await fetchJson(caller, `${ORCHESTRATOR_URL}/query`, {
      method: "POST",
      body: JSON.stringify({
        question: "What meetings or events are scheduled for today?",
        limit: 3,
      }),
    });
    const data = { calendar: res?.answer || null };
    setCache(caller, "calendar", data);
    return data;
  } catch {
    return { calendar: null };
  }
}

async function fetchOverview(caller: BackendCaller): Promise<Record<string, unknown>> {
  const [health, inbox] = await Promise.allSettled([
    fetchHealth(caller),
    fetchInbox(caller),
  ]);
  return {
    health: health.status === "fulfilled" ? health.value : { status: "unknown", services: [] },
    inbox: inbox.status === "fulfilled" ? inbox.value : { openCount: 0, unreadCount: 0 },
  };
}

/**
 * Fetch data from SearXNG for web search queries.
 * SearXNG is internal-only and unauthenticated — no caller credential is sent.
 */
export async function fetchWebSearch(query: string): Promise<Record<string, unknown>> {
  try {
    const params = new URLSearchParams({
      q: query,
      format: "json",
      categories: "general",
    });
    const res = await fetchJson(
      { headers: {}, scope: "anonymous" },
      `${SEARXNG_URL}/search?${params.toString()}`,
    );
    const results = (res?.results || []).slice(0, 8).map(
      (r: { title?: string; url?: string; content?: string }) => ({
        title: r.title || "",
        url: r.url || "",
        snippet: r.content || "",
      })
    );
    return { query, results, resultCount: results.length };
  } catch {
    return { query, results: [], resultCount: 0 };
  }
}

/**
 * Parse a dataNeeds array from the triage step and fetch all data in parallel.
 * Supports:
 *   "health", "contacts", "conversations", "companies", "calendar", "overview"
 *   "memory:<query>" — RAG search
 *   "web:<query>" — SearXNG web search
 */
const ALLOWED_PREFIXES = new Set([
  "health", "contacts", "conversations", "companies", "calendar",
  "memory", "web", "overview",
]);

export async function fetchDataForNeeds(
  dataNeeds: string[]
): Promise<Record<string, unknown>> {
  if (!dataNeeds.length) return {};

  const validNeeds = dataNeeds.filter((need) => {
    const prefix = need.split(":")[0];
    return ALLOWED_PREFIXES.has(prefix);
  });

  if (!validNeeds.length) return {};

  // One session lookup for the whole fan-out.
  const caller = await resolveCaller();

  const fetchers: Array<Promise<[string, Record<string, unknown>]>> = validNeeds.map(
    (need) => {
      const [prefix, ...rest] = need.split(":");
      const query = rest.join(":").trim().slice(0, 200);

      switch (prefix) {
        case "health":
          return fetchHealth(caller).then((d) => ["health", d] as [string, Record<string, unknown>]);
        case "contacts":
          return fetchContacts(caller).then((d) => ["contacts", d] as [string, Record<string, unknown>]);
        case "conversations":
          return fetchInbox(caller).then((d) => ["conversations", d] as [string, Record<string, unknown>]);
        case "companies":
          return fetchCompanies(caller).then((d) => ["companies", d] as [string, Record<string, unknown>]);
        case "calendar":
          return fetchCalendar(caller).then((d) => ["calendar", d] as [string, Record<string, unknown>]);
        case "overview":
          return fetchOverview(caller).then((d) => ["overview", d] as [string, Record<string, unknown>]);
        case "memory":
          return fetchMemory(caller, query || "recent information").then(
            (d) => ["memory", d] as [string, Record<string, unknown>]
          );
        case "web":
          return fetchWebSearch(query || "").then(
            (d) => ["web", d] as [string, Record<string, unknown>]
          );
        default:
          return Promise.resolve([prefix, {}] as [string, Record<string, unknown>]);
      }
    }
  );

  const results = await Promise.allSettled(fetchers);
  const merged: Record<string, unknown> = {};

  for (const result of results) {
    if (result.status === "fulfilled") {
      const [key, data] = result.value;
      merged[key] = data;
    }
  }

  return merged;
}

async function fetchJson(
  caller: BackendCaller,
  url: string,
  options?: RequestInit,
  retries = 1,
  timeoutMs = FETCH_TIMEOUT,
) {
  // Assert the resolved origin is a trusted backend before fetching (anti-SSRF).
  const target = new URL(url);
  if (!TRUSTED_ORIGINS.has(target.origin)) {
    throw new Error("Untrusted backend origin");
  }
  // The caller's credential goes only to backends that authenticate it.
  const authHeaders = AUTHENTICATED_ORIGINS.has(target.origin) ? caller.headers : {};
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(target.toString(), {
        ...options,
        headers: { "Content-Type": "application/json", ...authHeaders, ...options?.headers },
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      return res.json();
    } catch (err) {
      if (attempt < retries) {
        // Brief backoff before retry
        await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
        continue;
      }
      throw err;
    }
  }
}
