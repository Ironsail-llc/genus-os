/**
 * Topic-aware parallel data fetching for conversation-driven dashboards.
 * Server-side only — called from the dashboard generate API route.
 */

import { getServiceUrl } from "@/lib/services/registry";
const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";
const ORCHESTRATOR_URL = getServiceUrl("orchestrator") || "http://localhost:9099";
const VISION_URL = getServiceUrl("vision") || "http://localhost:8600";
const SEARXNG_URL = getServiceUrl("searxng") || "http://localhost:8888";
const FETCH_TIMEOUT = 5000;

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

function getCached(key: string): Record<string, unknown> | null {
  const entry = dataCache.get(key);
  if (!entry) return null;
  if (Date.now() > entry.expiresAt) { dataCache.delete(key); return null; }
  return entry.data;
}

function setCache(key: string, data: Record<string, unknown>) {
  const ttl = CACHE_TTL[key] ?? 60_000;
  dataCache.set(key, { data, expiresAt: Date.now() + ttl });
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

  try {
    switch (topic) {
      case "contacts":
        data = await fetchContacts();
        break;
      case "inbox":
        data = await fetchInbox();
        break;
      case "health":
        data = await fetchHealth();
        break;
      case "memory":
        data = await fetchMemory(searchQuery || "recent information");
        break;
      case "companies":
        data = await fetchCompanies();
        break;
      case "calendar":
        data = await fetchCalendar();
        break;
      case "overview":
        data = await fetchOverview();
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

async function fetchContacts(): Promise<Record<string, unknown>> {
  const cached = getCached("contacts");
  if (cached) return cached;
  try {
    const res = await fetchJson(`${BRIDGE_URL}/api/people?limit=20`);
    const data = { people: res?.data || [] };
    setCache("contacts", data);
    return data;
  } catch {
    return { people: [] };
  }
}

async function fetchInbox(): Promise<Record<string, unknown>> {
  const cached = getCached("conversations");
  if (cached) return cached;
  try {
    const res = await fetchJson(`${BRIDGE_URL}/api/conversations?status=open`);
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
    setCache("conversations", data);
    return data;
  } catch {
    return { conversations: [], openCount: 0, unreadCount: 0 };
  }
}

async function fetchHealth(): Promise<Record<string, unknown>> {
  const cached = getCached("health");
  if (cached) return cached;
  const checks = await Promise.allSettled([
    fetchJson(`${BRIDGE_URL}/health`),
    fetchJson(`${ORCHESTRATOR_URL}/health`),
    fetchJson(`${VISION_URL}/health`),
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
  setCache("health", data);
  return data;
}

async function fetchMemory(query: string): Promise<Record<string, unknown>> {
  try {
    const res = await fetchJson(`${ORCHESTRATOR_URL}/query`, {
      method: "POST",
      body: JSON.stringify({ question: query, limit: 5 }),
    });
    return { answer: res?.answer || null, query };
  } catch {
    return { answer: null, query };
  }
}

async function fetchCompanies(): Promise<Record<string, unknown>> {
  const cached = getCached("companies");
  if (cached) return cached;
  try {
    const [people, companies] = await Promise.allSettled([
      fetchJson(`${BRIDGE_URL}/api/people?limit=20`),
      fetchJson(`${BRIDGE_URL}/api/companies?limit=20`),
    ]);
    const data = {
      people: people.status === "fulfilled" ? people.value?.data || [] : [],
      companies: companies.status === "fulfilled" ? companies.value?.data || [] : [],
    };
    setCache("companies", data);
    return data;
  } catch {
    return { people: [], companies: [] };
  }
}

async function fetchCalendar(): Promise<Record<string, unknown>> {
  const cached = getCached("calendar");
  if (cached) return cached;
  try {
    const res = await fetchJson(`${ORCHESTRATOR_URL}/query`, {
      method: "POST",
      body: JSON.stringify({
        question: "What meetings or events are scheduled for today?",
        limit: 3,
      }),
    });
    const data = { calendar: res?.answer || null };
    setCache("calendar", data);
    return data;
  } catch {
    return { calendar: null };
  }
}

async function fetchOverview(): Promise<Record<string, unknown>> {
  const [health, inbox] = await Promise.allSettled([
    fetchHealth(),
    fetchInbox(),
  ]);
  return {
    health: health.status === "fulfilled" ? health.value : { status: "unknown", services: [] },
    inbox: inbox.status === "fulfilled" ? inbox.value : { openCount: 0, unreadCount: 0 },
  };
}

/**
 * Fetch data from SearXNG (localhost:8888) for web search queries.
 * SearXNG is internal-only, no API key needed.
 */
export async function fetchWebSearch(query: string): Promise<Record<string, unknown>> {
  try {
    const params = new URLSearchParams({
      q: query,
      format: "json",
      categories: "general",
    });
    const res = await fetchJson(`${SEARXNG_URL}/search?${params.toString()}`);
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
 * Fetch data from Impetus One via Bridge proxy.
 * Uses a tight 2s timeout with no retries — Impetus data is supplemental,
 * not worth blocking dashboard generation for.
 *
 * Returns slimmed data to fit within the prompt budget — raw JSON-LD is too
 * bloated (38KB for 23 prescriptions). We strip @context, @type, IRI refs,
 * and verbose clinical notes, keeping only dashboard-renderable fields.
 */
const IMPETUS_TIMEOUT = 2000;
async function fetchImpetusData(resource: string): Promise<Record<string, unknown>> {
  try {
    const res = await fetchJson(`${BRIDGE_URL}/api/impetus/${resource}`, undefined, 0, IMPETUS_TIMEOUT);
    return { [resource]: slimImpetusResponse(resource, res) };
  } catch {
    return { [resource]: [] };
  }
}

/* eslint-disable @typescript-eslint/no-explicit-any */
function slimImpetusResponse(resource: string, raw: any): any {
  if (!raw) return raw;
  const members: any[] = raw?.["hydra:member"] ?? raw?.member ?? (Array.isArray(raw) ? raw : []);
  if (!members.length) return raw;

  switch (resource) {
    case "prescriptions": {
      // Summary counts by status
      const counts: Record<string, number> = {};
      for (const rx of members) {
        const s = rx.status ?? "unknown";
        counts[s] = (counts[s] || 0) + 1;
      }
      // Slim per-prescription details
      const items = members.map((rx: any) => {
        const med = typeof rx.medication === "object" ? rx.medication : {};
        const pharm = typeof rx.pharmacy === "object" ? rx.pharmacy : {};
        const pat = typeof rx.patient === "object" ? rx.patient : {};
        return {
          id: rx.id,
          patient: pat.firstName && pat.lastName ? `${pat.firstName} ${pat.lastName}` : (pat.firstName || rx.patient),
          medication: med.drugName || "Unknown",
          strength: med.strength || null,
          controlled: med.isControlled || false,
          schedule: med.schedule || null,
          pharmacy: pharm.name || null,
          pharmacyCity: pharm.city || null,
          status: rx.status,
          quantity: rx.quantity,
          daysSupply: rx.daysSupply,
          refills: rx.refills,
          directions: rx.directions,
          prescribedAt: rx.prescribedAt || null,
          transmittedAt: rx.transmittedAt || null,
          filledAt: rx.filledAt || null,
          shippedAt: rx.shippedAt || null,
          trackingNumber: rx.trackingNumber || null,
          carrier: rx.carrier || null,
          clinicalNotes: rx.clinicalNotes ? (rx.clinicalNotes as string).slice(0, 120) : null,
        };
      });
      return { totalItems: members.length, statusCounts: counts, prescriptions: items };
    }
    case "patients": {
      const items = members.map((p: any) => ({
        id: p.id,
        firstName: p.firstName,
        lastName: p.lastName,
        dob: p.dob,
        gender: p.gender,
        allergies: p.allergies || null,
        medicalConditions: p.medicalConditions || null,
        phone: p.phone || null,
        email: p.email || null,
      }));
      return { totalItems: members.length, patients: items };
    }
    case "appointments": {
      const items = members.map((a: any) => {
        const pat = typeof a.patient === "object" ? a.patient : {};
        const prov = typeof a.provider === "object" ? a.provider : {};
        return {
          id: a.id,
          patient: pat.firstName && pat.lastName ? `${pat.firstName} ${pat.lastName}` : (pat.firstName || a.patient),
          provider: prov.firstName && prov.lastName ? `Dr. ${prov.firstName} ${prov.lastName}` : a.provider,
          scheduledAt: a.scheduledAt,
          type: a.type,
          status: a.status,
          reason: a.reason,
          durationMinutes: a.durationMinutes,
          patientNotes: a.patientNotes ? (a.patientNotes as string).slice(0, 100) : null,
        };
      });
      return { totalItems: members.length, appointments: items };
    }
    default:
      // For other resources, just extract members from JSON-LD wrapper
      return Array.isArray(raw) ? raw : members;
  }
}
/* eslint-enable @typescript-eslint/no-explicit-any */

/**
 * Parse a dataNeeds array from the triage step and fetch all data in parallel.
 * Supports:
 *   "health", "contacts", "conversations", "companies", "calendar", "overview"
 *   "memory:<query>" — RAG search
 *   "web:<query>" — SearXNG web search
 *   "prescriptions", "patients", "queue", "orders", "pharmacy", "appointments:io" — Impetus One
 */
const ALLOWED_PREFIXES = new Set([
  "health", "contacts", "conversations", "companies", "calendar",
  "memory", "web", "overview", "prescriptions", "patients", "queue",
  "orders", "pharmacy", "appointments", "medications", "encounters",
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

  const fetchers: Array<Promise<[string, Record<string, unknown>]>> = validNeeds.map(
    (need) => {
      const [prefix, ...rest] = need.split(":");
      const query = rest.join(":").trim().slice(0, 200);

      switch (prefix) {
        case "health":
          return fetchHealth().then((d) => ["health", d] as [string, Record<string, unknown>]);
        case "contacts":
          return fetchContacts().then((d) => ["contacts", d] as [string, Record<string, unknown>]);
        case "conversations":
          return fetchInbox().then((d) => ["conversations", d] as [string, Record<string, unknown>]);
        case "companies":
          return fetchCompanies().then((d) => ["companies", d] as [string, Record<string, unknown>]);
        case "calendar":
          return fetchCalendar().then((d) => ["calendar", d] as [string, Record<string, unknown>]);
        case "overview":
          return fetchOverview().then((d) => ["overview", d] as [string, Record<string, unknown>]);
        case "memory":
          return fetchMemory(query || "recent information").then(
            (d) => ["memory", d] as [string, Record<string, unknown>]
          );
        case "web":
          return fetchWebSearch(query || "").then(
            (d) => ["web", d] as [string, Record<string, unknown>]
          );
        // Impetus One data sources
        case "prescriptions":
          return fetchImpetusData("prescriptions").then(
            (d) => ["prescriptions", d] as [string, Record<string, unknown>]
          );
        case "patients":
          return fetchImpetusData("patients").then(
            (d) => ["patients", d] as [string, Record<string, unknown>]
          );
        case "queue":
          return fetchImpetusData("queue").then(
            (d) => ["queue", d] as [string, Record<string, unknown>]
          );
        case "orders":
          return fetchImpetusData("orders").then(
            (d) => ["orders", d] as [string, Record<string, unknown>]
          );
        case "pharmacy":
          return fetchImpetusData("pharmacies").then(
            (d) => ["pharmacy", d] as [string, Record<string, unknown>]
          );
        case "appointments":
          if (query === "io") {
            return fetchImpetusData("appointments").then(
              (d) => ["appointments", d] as [string, Record<string, unknown>]
            );
          }
          return Promise.resolve(["appointments", {}] as [string, Record<string, unknown>]);
        case "medications":
          return fetchImpetusData("medications").then(
            (d) => ["medications", d] as [string, Record<string, unknown>]
          );
        case "encounters":
          return fetchImpetusData("encounters").then(
            (d) => ["encounters", d] as [string, Record<string, unknown>]
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

async function fetchJson(url: string, options?: RequestInit, retries = 1, timeoutMs = FETCH_TIMEOUT) {
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url, {
        ...options,
        headers: { "Content-Type": "application/json", ...options?.headers },
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
