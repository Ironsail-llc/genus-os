"use client";

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export interface RecoveredPlan {
  plan_id: string;
  plan_text: string;
  original_message: string;
  status: string;
  deep_plan?: boolean;
}

export interface ChatRecoveryRequest {
  requestId: string;
  agent: string;
  scope?: string;
}

/** Reattach delivery by reading the original run; this component never executes work. */
export function ChatRecovery({ request, messageId, onRecovered }: {
  messageId: string;
  request: ChatRecoveryRequest;
  onRecovered: (id: string, text: string, plan?: RecoveredPlan) => void;
}) {
  const [status, setStatus] = useState("Connection interrupted. Checking the recorded result…");
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    async function check() {
      const params = new URLSearchParams({ request_id: request.requestId });
      if (request.agent) params.set("agent", request.agent);
      try {
        const response = await fetch(`/api/chat/outcome?${params}`, {
          cache: "no-store",
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10_000)]),
        });
        if (!response.ok) throw new Error("Audit record unavailable");
        const record = await response.json();
        if (controller.signal.aborted) return;
        if (record.terminal === true && record.reconciliation_pending !== true && typeof record.text === "string") {
          const plan = record.plan;
          const savedPlan = plan?.status === "pending" &&
            [plan.plan_id, plan.plan_text, plan.original_message].every(value => typeof value === "string")
            ? plan as RecoveredPlan : undefined;
          const text = record.text || "The run has finished; no response text was recorded.";
          if (savedPlan) onRecovered(messageId, text, savedPlan);
          else onRecovered(messageId, text);
          return;
        }
        setStatus(record.reconciliation_pending === true
          ? `${typeof record.text === "string" ? record.text : ""}\n\nChecking for updated action evidence…`
          : record.state === "running"
            ? "The original run is still working. Waiting for its recorded result…"
            : record.state === "accepted"
              ? "Your approval is recorded. Checking whether execution has started…"
            : record.state === "awaiting_approval"
              ? "The original run is waiting for approval. Checking for its result…"
              : "Checking the original request’s audit record…");
      } catch {
        if (controller.signal.aborted) return;
        setStatus("Reconnecting to read the original request’s audit record…");
      }
      timer = setTimeout(check, Math.min(10_000, 1000 * 2 ** Math.min(attempts++, 4)));
    }
    void check();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [request, messageId, onRecovered]);
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{status}</ReactMarkdown>;
}
