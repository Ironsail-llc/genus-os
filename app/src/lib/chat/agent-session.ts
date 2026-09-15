/**
 * Which agent the chat is talking to, and the one place that becomes a session
 * key.
 *
 * The engine already knows how to route a chat request to any agent: `chat.py`'s
 * `_effective_session_key` resolves an EMPTY `session_key` to
 * `EngineConfig.main_session_key`, keeps an owner's requested key verbatim, and
 * (under `per_user_sessions_mode() == "enforce"`) isolates a member onto
 * `agent:{id}:user:{user_id}` by reading `parts[1]` of a key shaped
 * `agent:<id>:primary`. So the only thing the Helm has to get right is the key
 * — and, much more importantly, when NOT to send one.
 *
 * That is the rule this module exists to hold:
 *
 * **The default agent sends no key at all.** Not `agent:main:primary`, not an
 * empty string in the body — nothing. The operator's webchat and Telegram share
 * one session on purpose (`telegram.py`'s `main_session_key`), every message
 * they have ever sent lives in it, and the Helm has spelled that session by
 * omission since B1. A browser that started naming it explicitly would be right
 * until the day an instance sets `ROBOTHOR_DEFAULT_CHAT_AGENT`, and then the
 * operator's history would be sitting in a session the app no longer opens.
 * Which id is the default is therefore a question only the appliance can
 * answer: `GET /api/agent-manifests` reports it as `default_agent`, and the
 * panel sends `agent` only for somebody else.
 *
 * The id→key direction is also a boundary. A browser sends an agent ID; it
 * never sends a session key. If it could spell one it could spell
 * `agent:main:user:somebody-else`, and while `_effective_session_key` would
 * refuse that for a member, an owner's key is kept verbatim by design. So an id
 * that is not a plain id is not sanitized into one — it produces no key, and the
 * request lands on the main session, which is the safe direction to fail in.
 */

/** Where the browser remembers the choice. Per browser, never in the URL. */
export const CHAT_AGENT_STORAGE_KEY = "helm.chat.agent";

/**
 * What a manifest id must look like *for this one purpose*.
 *
 * This is not a charset policy for agent ids, and it must not become one.
 * Nothing validates ids on the read path — `load_manifest_dir` returns the
 * documents as written, `manifest_schema.py` has no charset rule, and the
 * bridge's `validate_identifier` guards only the WRITE routes. So `acme.bot` is
 * a real, listable, runnable agent.
 *
 * The earlier version here refused it and returned `""`, and `""` means "the
 * main session". A browser refusing an id the appliance itself offered wrote a
 * private message to a worker into the operator's shared history — failing
 * "safe" in the one direction that merges two conversations. The allowlist is
 * the listing; the only thing judged here is whether the id still fits the key
 * shape `_effective_session_key` parses.
 *
 * Which is: no `:` (the key's own delimiter — an id carrying one produces
 * `agent:a:primary:b:primary`, a shape nothing parses), and no whitespace or
 * control characters, which no manifest id has and a header value must not.
 */
const UNKEYABLE = /[\s:\u0000-\u001f\u007f]/;
const MAX_AGENT_ID = 128;

/** Whether `agent` can be turned into a well-formed session key at all. */
export function isKeyableAgentId(agent: unknown): boolean {
  if (typeof agent !== "string") return false;
  const id = agent.trim();
  return id.length > 0 && id.length <= MAX_AGENT_ID && !UNKEYABLE.test(id);
}

/**
 * The session key that reaches `agent`, or `""` for "let the engine decide".
 *
 * `""` is returned ONLY for an absent choice. A non-empty id that cannot be
 * keyed also returns `""` here, which is why no caller may use this alone to
 * decide what to send: see `resolveAgentSessionKey`, which tells those two
 * cases apart so a request that cannot be honoured is refused instead of
 * silently landing on the main session.
 */
export function sessionKeyForAgent(agent: unknown): string {
  if (!isKeyableAgentId(agent)) return "";
  return `agent:${(agent as string).trim()}:primary`;
}

/**
 * The same decision, with the two failure modes kept apart.
 *
 * - nothing chosen (absent, or an empty/blank string) →
 *   `{ chosen: false, key: "" }`, send no key;
 * - a usable id → `{ chosen: true, key: "agent:<id>:primary" }`;
 * - anything else present — an unkeyable id, a number, an object — →
 *   `{ chosen: true, key: "" }`, which every caller must treat as a refusal.
 *   Falling through to the main session is the merge this module exists to
 *   prevent, and "the field was the wrong type so we ignored it" lands there
 *   just as squarely as a bad id does.
 */
export function resolveAgentSessionKey(agent: unknown): { chosen: boolean; key: string } {
  const absent =
    agent === undefined ||
    agent === null ||
    (typeof agent === "string" && agent.trim().length === 0);
  return { chosen: !absent, key: sessionKeyForAgent(agent) };
}

/** The agent id a request should carry, or `""` when it must carry none. */
export function normalizeAgentId(agent: unknown): string {
  return isKeyableAgentId(agent) ? (agent as string).trim() : "";
}

/**
 * The remembered choice, or `""`.
 *
 * Every access is wrapped: a private window, blocked site data and a browser
 * that throws on `localStorage` are all real, and none of them is a reason for
 * the chat not to open. Failing to `""` also fails toward the main session.
 */
export function readStoredChatAgent(): string {
  try {
    if (typeof window === "undefined") return "";
    return normalizeAgentId(window.localStorage.getItem(CHAT_AGENT_STORAGE_KEY));
  } catch {
    return "";
  }
}

/** Remember the choice. The default (`""`) is remembered by forgetting. */
export function storeChatAgent(agent: string): void {
  try {
    if (typeof window === "undefined") return;
    const id = normalizeAgentId(agent);
    if (id) {
      window.localStorage.setItem(CHAT_AGENT_STORAGE_KEY, id);
    } else {
      window.localStorage.removeItem(CHAT_AGENT_STORAGE_KEY);
    }
  } catch {
    // A browser that will not remember is a browser that starts on the main
    // agent next time. Nothing else about the chat changes.
  }
}
