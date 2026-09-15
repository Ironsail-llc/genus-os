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
 */

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

function bridgeUrl(): string {
  return getServiceUrl("bridge") || "http://localhost:9100";
}

/** Resolve `agent` into the session key to forward, or the refusal to answer. */
export async function resolveChatAgent(agent: unknown): Promise<ChatAgentVerdict> {
  const { chosen, key } = resolveAgentSessionKey(agent);
  if (!chosen) return { ok: true, key: "" };
  if (!key) return { ok: false, status: 400, error: UNKEYABLE };

  const id = (agent as string).trim();

  let payload: { agents?: ManifestRow[]; default_agent?: unknown } | null = null;
  try {
    const res = await fetch(`${bridgeUrl()}/api/agent-manifests`, {
      headers: await bridgeAuthHeaders(),
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) return { ok: false, status: 403, error: NOT_YOURS };
    payload = await res.json();
  } catch {
    // Unreachable, timed out, or a body that is not JSON. None of those is
    // permission.
    return { ok: false, status: 403, error: NOT_YOURS };
  }

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
