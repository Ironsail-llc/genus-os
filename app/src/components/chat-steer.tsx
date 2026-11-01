"use client";

/**
 * Steer mode for the Helm chat (ROBOTHOR_STEER_EVERYWHERE).
 *
 * While a reply streams, what is typed goes to the running task as an operator
 * steering update (it joins the run at its next step; it is not a new turn),
 * and Stop stops this tab's run the way Telegram's Interrupt does instead of
 * cancelling the request.
 * With the switch off the composer keeps its follow-up and abort behaviour.
 */
import { useEffect, useState } from "react";

export const STEER_PLACEHOLDER = "Steer the running task...";
export const STEERED_NOTE = "Steering the run";

/** True once the engine says steer mode is on. False until then and on any error. */
export function useSteerMode(): boolean {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let live = true;
    fetch("/api/chat/steer")
      .then((res) => (res.ok ? res.json() : null))
      .then((body) => { if (live) setEnabled(body?.enabled === true); })
      .catch(() => {});
    return () => { live = false; };
  }, []);
  return enabled;
}

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
