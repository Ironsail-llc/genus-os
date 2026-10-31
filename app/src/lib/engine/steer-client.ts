/**
 * Server-side calls for steering and stopping a live chat run.
 * Used by the Next.js API routes only (robothor/engine/chat.py: /chat/steer,
 * /chat/interrupt). Kept beside server-client.ts rather than in it so the
 * feature stays one switch (ROBOTHOR_STEER_EVERYWHERE) and one set of files.
 */
import { bridgeAuthHeaders } from "@/lib/bridge-auth";

const ENGINE_URL = process.env.ROBOTHOR_ENGINE_URL || "http://127.0.0.1:18800";

export type SteerResult = { ok: boolean; found: boolean };

async function post(path: string, body: Record<string, unknown>): Promise<SteerResult> {
  const res = await fetch(`${ENGINE_URL}${path}`, {
    method: "POST",
    headers: { ...(await bridgeAuthHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) throw new Error(`Engine error: ${res.status} ${res.statusText}`);
  return res.json();
}

/** The same rule as server-client's `keyed`: no key means the main session. */
function keyed(sessionKey: string): { session_key?: string } {
  return sessionKey ? { session_key: sessionKey } : {};
}

/** Add `text` to the live run of the turn `requestId` names, at its next step. */
export async function chatSteer(sessionKey: string, text: string, requestId: string): Promise<SteerResult> {
  return post("/chat/steer", { ...keyed(sessionKey), text, request_id: requestId });
}

/** Stop the live run of the turn `requestId` names. */
export async function chatInterrupt(sessionKey: string, requestId: string): Promise<SteerResult> {
  return post("/chat/interrupt", { ...keyed(sessionKey), request_id: requestId });
}

/** Whether the engine has steer mode on. */
export async function chatSteerEnabled(): Promise<{ enabled: boolean }> {
  const res = await fetch(`${ENGINE_URL}/chat/steer`, {
    headers: await bridgeAuthHeaders(),
    signal: AbortSignal.timeout(10_000),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`Engine error: ${res.status}`);
  const body = await res.json();
  return { enabled: body?.enabled === true };
}
