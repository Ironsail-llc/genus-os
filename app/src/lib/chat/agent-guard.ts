/**
 * Server-side: may THIS caller address the agent they named?
 *
 * `chattable` on the fleet listing draws the switcher. It does not authorize
 * anything, and it must not be mistaken for authorization: the BFF forwards to
 * `chat.py`, whose `_require_chat_auth` asks only that the caller be
 * authenticated, and which then takes `agent_id` straight out of `parts[1]` of
 * the session key and runs that agent. So before this guard existed, a
 * hand-written `POST /api/chat/send {"agent":"whatever"}` from any signed-in
 * user reached any agent on the appliance. Tool RBAC still travels with
 * `user_role`, so that was not a tool-permission escalation — but it was a
 * capability non-operators did not previously have, in a PR whose own report
 * claimed members were unaffected.
 *
 * The authority is the same listing the switcher reads, fetched with the
 * CALLER's own bridge credentials. That makes the rule self-enforcing rather
 * than duplicated: `GET /api/agent-manifests` is operator-gated, so a member's
 * listing is refused, so a member may name no agent at all. Nothing here
 * re-derives "who counts as an operator"; drift between two copies of that rule
 * is how a gate stops meaning what its tests say it means.
 *
 * Three refusals, all of them explicit, none of them falling through to the
 * main session:
 *
 * - **400** — a named agent whose id cannot become a well-formed session key.
 *   The earlier version returned the empty key here, and the empty key means
 *   the operator's shared main session: a private message to a worker appended
 *   to the conversation the Helm and Telegram share.
 * - **403** — a named agent the caller's own listing does not offer as
 *   chattable, including every agent when the listing itself is refused.
 * - **403** — the bridge unreachable. Fail closed; "we could not check" is not
 *   "allowed".
 *
 * The check costs one bridge call and is skipped entirely when no agent was
 * named, which is every request the default chat makes — so the common path,
 * the operator talking to their own main agent, is unchanged.
 *
 * That one call is cached per server instance for a short while
 * (`GENUS_CHAT_AGENT_CACHE_TTL_MS`, default 30 s, `0` turns it off), because
 * the bridge builds the listing by rescanning every manifest and a chat with a
 * named agent asked for it on every message. The cache is keyed by a hash of
 * the caller's credentials, so one caller's listing never answers for another;
 * only a listing the bridge actually served is kept, so a refusal or an outage
 * is asked again next time; and any manifest or agent change made through the
 * bridge proxy forgets it ({@link forgetChatAgentListings}). The cost is that
 * an agent made unchattable some other way stays addressable for at most one
 * TTL on an instance that had just cached it.
 */

import { createHash } from "node:crypto";

import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { getServiceUrl } from "@/lib/services/registry";
import { resolveAgentSessionKey } from "@/lib/chat/agent-session";

export type ChatAgentVerdict =
  | { ok: true; key: string }
  | { ok: false; status: 400 | 403; error: string };

const UNKEYABLE = "agent must be a plain agent id";
const NOT_YOURS = "that agent is not one this account may chat with";

interface ManifestRow {
  id?: unknown;
  chattable?: unknown;
}

interface ManifestListing {
  agents?: ManifestRow[];
  default_agent?: unknown;
}

function bridgeUrl(): string {
  return getServiceUrl("bridge") || "http://localhost:9100";
}

const DEFAULT_LISTING_TTL_MS = 30_000;

/** Served listings by caller, each with the time it stops being trusted. */
const listings = new Map<string, { listing: ManifestListing; expires: number }>();

function listingTtlMs(): number {
  const raw = process.env.GENUS_CHAT_AGENT_CACHE_TTL_MS;
  if (raw === undefined || raw.trim() === "") return DEFAULT_LISTING_TTL_MS;
  const ttl = Number(raw);
  return Number.isFinite(ttl) && ttl > 0 ? ttl : 0;
}

/** The caller, as the bridge will see them, without keeping their token in memory twice. */
function callerKey(headers: Record<string, string>): string {
  const canonical = Object.entries(headers)
    .map(([name, value]) => [name.toLowerCase(), value])
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return createHash("sha256").update(JSON.stringify(canonical)).digest("hex");
}

/** Forget every cached listing: a manifest or an agent changed. */
export function forgetChatAgentListings(): void {
  listings.clear();
}

/** The caller's listing, from the cache or the bridge; null when refused or unreachable. */
async function callerListing(): Promise<ManifestListing | null> {
  const headers = await bridgeAuthHeaders();
  const ttl = listingTtlMs();
  const key = callerKey(headers);
  const now = Date.now();
  const cached = ttl > 0 ? listings.get(key) : undefined;
  if (cached && cached.expires > now) return cached.listing;

  try {
    const res = await fetch(`${bridgeUrl()}/api/agent-manifests`, {
      headers,
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) return null;
    const listing = ((await res.json()) ?? {}) as ManifestListing;
    if (ttl > 0) {
      for (const [k, entry] of listings) if (entry.expires <= now) listings.delete(k);
      listings.set(key, { listing, expires: now + ttl });
    }
    return listing;
  } catch {
    // Unreachable, timed out, or a body that is not JSON. None of those is
    // permission.
    return null;
  }
}

/** Resolve `agent` into the session key to forward, or the refusal to answer. */
export async function resolveChatAgent(agent: unknown): Promise<ChatAgentVerdict> {
  const { chosen, key } = resolveAgentSessionKey(agent);
  if (!chosen) return { ok: true, key: "" };
  if (!key) return { ok: false, status: 400, error: UNKEYABLE };

  const id = (agent as string).trim();

  const payload = await callerListing();
  if (!payload) return { ok: false, status: 403, error: NOT_YOURS };

  // Naming the main agent explicitly is allowed and still sends no key: the
  // rule is about which session gets written to, not which spelling arrived.
  if (typeof payload?.default_agent === "string" && payload.default_agent === id) {
    return { ok: true, key: "" };
  }

  const allowed =
    Array.isArray(payload?.agents) &&
    payload.agents.some((row) => row?.chattable === true && String(row?.id ?? "") === id);

  return allowed ? { ok: true, key } : { ok: false, status: 403, error: NOT_YOURS };
}
