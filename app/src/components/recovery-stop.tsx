"use client";

import { useEffect, useRef, useState } from "react";
import type { ChatRecoveryRequest } from "./chat-recovery";

/** Stop the original request without depending on the active composer. */
export function RecoveryStop({ request, recorded }: { request: ChatRecoveryRequest; recorded: boolean }) {
  const [state, setState] = useState<"idle" | "sending" | "confirmed" | "unconfirmed">("idle");
  const pending = useRef(false);
  const controller = useRef(new AbortController());
  useEffect(() => {
    controller.current = new AbortController();
    return () => controller.current.abort();
  }, []);

  async function stop() {
    if (pending.current || recorded || state === "confirmed") return;
    pending.current = true;
    setState("sending");
    const lifecycle = controller.current.signal;
    try {
      const response = await fetch("/api/chat/abort", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: request.requestId, ...(request.agent ? { agent: request.agent } : {}) }),
        signal: AbortSignal.any([lifecycle, AbortSignal.timeout(10_000)]),
      });
      if (!response.ok) throw new Error("Stop unconfirmed");
      const result = await response.json();
      if (result.ok !== true || result.durable_stopped !== true) throw new Error("Stop unconfirmed");
      if (!lifecycle.aborted) setState("confirmed");
    } catch {
      if (!lifecycle.aborted) setState("unconfirmed");
    } finally {
      pending.current = false;
    }
  }

  if (recorded || state === "confirmed") return <p role="status" className="mt-2 text-xs text-muted-foreground">Stop recorded.</p>;
  return <div className="mt-2">
    <button type="button" onClick={stop} disabled={state === "sending"}
      className="rounded border px-2 py-1 text-xs disabled:opacity-50" data-testid="recovery-stop">
      {state === "sending" ? "Stopping…" : "Stop this request"}
    </button>
    {state === "unconfirmed" && <p role="status" className="mt-1 text-xs text-muted-foreground">Stop has not been confirmed. Checking the original request; you can try Stop again.</p>}
  </div>;
}
