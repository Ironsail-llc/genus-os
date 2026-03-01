/**
 * Central identity config — server-side only.
 *
 * All identity references (owner name, AI name, session key, agent ID)
 * are derived from environment variables with sensible defaults.
 */

export const OWNER_NAME = process.env.ROBOTHOR_OWNER_NAME || "there";
export const AI_NAME = process.env.ROBOTHOR_AI_NAME || "Robothor";

export const SESSION_KEY =
  process.env.AGENT_SESSION_KEY || "agent:main:primary";

export const HELM_AGENT_ID = process.env.HELM_AGENT_ID || "helm-user";
