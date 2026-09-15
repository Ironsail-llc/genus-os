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
 * What a manifest id is allowed to look like here.
 *
 * Deliberately narrower than the bridge's `_safe_id`: this value is
 * interpolated into a colon-delimited key the engine then SPLITS on colons, so
 * the one character that must never appear is the delimiter. Letters, digits,
 * `-` and `_` cover every id `genus` will generate.
 */
const AGENT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

/** The session key that reaches `agent`, or `""` for "let the engine decide". */
export function sessionKeyForAgent(agent: unknown): string {
  if (typeof agent !== "string") return "";
  const id = agent.trim();
  if (!AGENT_ID.test(id)) return "";
  return `agent:${id}:primary`;
}

/** The agent id a request should carry, or `""` when it must carry none. */
export function normalizeAgentId(agent: unknown): string {
  if (typeof agent !== "string") return "";
  const id = agent.trim();
  return AGENT_ID.test(id) ? id : "";
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
