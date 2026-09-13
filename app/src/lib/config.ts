/**
 * Central identity config — server-side only.
 *
 * All identity references (owner name, AI name, agent ID) are derived from
 * environment variables with sensible defaults.
 *
 * There is deliberately no session key here any more. `AGENT_SESSION_KEY` used
 * to name one shared engine session that every dashboard user's traffic went
 * into, and the app sent that literal string on every chat call. The engine now
 * derives the session from the authenticated caller
 * (`robothor/engine/chat.py::_effective_session_key`, per-user by default), so
 * a key sent from here could only ever be ignored or — worse — honoured, which
 * is a browser choosing whose conversation it joins.
 */

export const OWNER_NAME = process.env.ROBOTHOR_OWNER_NAME || "there";
export const AI_NAME = process.env.ROBOTHOR_AI_NAME || "Robothor";

export const HELM_AGENT_ID = process.env.HELM_AGENT_ID || "helm-user";
