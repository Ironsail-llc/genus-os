/**
 * Server-side calls for steering and stopping a live chat run.
 * Used by the Next.js API routes only (robothor/engine/chat.py: /chat/steer,
 * /chat/interrupt). Kept beside server-client.ts rather than in it so the
 * feature stays one set of files.
 */
import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { engineUrlForAuthorization } from "@/lib/services/registry";

export type SteerResult = { ok: boolean; found: boolean };

/**
 * One POST to the engine of the tenant the session acts in, chosen from its
 * bearer exactly as server-client's chat calls choose theirs: a child tenant's
 * session sent to the root engine is refused there.
 */
async function post(path: string, body: Record<string, unknown>): Promise<SteerResult> {
  const headers: Record<string, string> = {
    ...(await bridgeAuthHeaders()),
    "Content-Type": "application/json",
  };
  const res = await fetch(`${engineUrlForAuthorization(headers.Authorization)}${path}`, {
    method: "POST",
    headers,
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
