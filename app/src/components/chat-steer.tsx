"use client";

/**
 * Steering for the Helm chat.
 *
 * While a reply streams, what is typed goes to the running task as an operator
 * steering update (it joins the run at its next step; it is not a new turn),
 * and Stop stops this tab's run the way Telegram's Interrupt does instead of
 * cancelling the request. When the turn has no live run to reach, the composer
 * falls back to a follow-up and Stop to an abort.
 */

export const STEER_PLACEHOLDER = "Steer the running task...";
export const STEERED_NOTE = "Steering the run";

async function call(url: string, body: Record<string, unknown>): Promise<boolean> {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) return false;
    const result = await res.json();
    return result?.found === true;
  } catch {
    return false;
  }
}

/**
 * Steer the running task. True when a live run took it.
 * `requestId` names the turn: two tabs on one agent are two runs.
 */
export function steerRun(text: string, agent: string, requestId: string | null): Promise<boolean> {
  if (!requestId) return Promise.resolve(false);
  return call("/api/chat/steer", { text, request_id: requestId, ...(agent ? { agent } : {}) });
}

/** Halt the running task (this tab's turn only). True when its live run was found. */
export function interruptRun(agent: string, requestId: string | null): Promise<boolean> {
  if (!requestId) return Promise.resolve(false);
  return call("/api/chat/interrupt", { request_id: requestId, ...(agent ? { agent } : {}) });
}
