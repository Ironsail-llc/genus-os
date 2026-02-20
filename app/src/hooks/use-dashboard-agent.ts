"use client";

import { useEffect, useRef, useCallback } from "react";
import { useVisualState } from "@/hooks/use-visual-state";
import { validateDashboardCode } from "@/lib/dashboard/code-validator";

const DEBOUNCE_MS = 300;

/**
 * Background dashboard update agent.
 * Watches pendingMessages from the visual state context, debounces,
 * calls the triage+generate API, and atomically swaps the dashboard
 * when the new HTML is ready.
 *
 * The current dashboard stays visible throughout — only a spinner
 * overlay indicates work in progress.
 */
export function useDashboardAgent() {
  const {
    pendingMessages,
    pendingAgentData,
    isUpdating,
    setIsUpdating,
    setDashboardCode,
    setCanvasMode,
    dashboardCode,
  } = useVisualState();

  const abortRef = useRef<AbortController | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleUpdate = useCallback(
    async (messages: Array<{ role: string; content: string }>, agentData?: Record<string, unknown> | null) => {
      // Cancel any in-flight request
      abortRef.current?.abort();
      const abort = new AbortController();
      abortRef.current = abort;

      try {
        const body: Record<string, unknown> = { messages };
        if (agentData && Object.keys(agentData).length > 0) {
          body.agentData = agentData;
        }

        console.log("[dashboard-agent] Requesting update, messages:", messages.length, "agentData:", agentData ? Object.keys(agentData) : "none");

        const res = await fetch("/api/dashboard/generate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: abort.signal,
        });

        if (abort.signal.aborted) return;

        if (res.status === 204) {
          console.log("[dashboard-agent] Triage: no update needed (204)");
          setIsUpdating(false);
          return;
        }

        if (!res.ok) {
          console.warn("[dashboard-agent] Generate failed:", res.status, await res.text().catch(() => ""));
          setIsUpdating(false);
          return;
        }

        const data = await res.json();

        if (abort.signal.aborted) return;

        if (data.html) {
          const validation = validateDashboardCode(data.html);
          if (validation.valid) {
            console.log("[dashboard-agent] Dashboard updated:", data.html.length, "chars");
            setDashboardCode(validation.code, data.type || "html");
          } else {
            console.warn("[dashboard-agent] Client validation failed:", validation.errors);
          }
        } else {
          console.warn("[dashboard-agent] Response missing html field:", Object.keys(data));
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          console.error("[dashboard-agent] Error:", err);
        }
      } finally {
        if (!abort.signal.aborted) {
          setIsUpdating(false);
        }
      }
    },
    [setDashboardCode, setIsUpdating]
  );

  // React to pendingMessages changes with debounce
  useEffect(() => {
    if (!pendingMessages || pendingMessages.length === 0) return;

    // Clear any pending debounce
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
    }

    // Capture agentData at debounce time so it's consistent with the messages
    const agentDataSnapshot = pendingAgentData;
    console.log("[dashboard-agent] Scheduling update in", DEBOUNCE_MS, "ms for", pendingMessages.length, "messages");
    debounceRef.current = setTimeout(() => {
      handleUpdate(pendingMessages, agentDataSnapshot);
    }, DEBOUNCE_MS);

    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, [pendingMessages, pendingAgentData, handleUpdate]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, []);
}
