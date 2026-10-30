"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import { useVisualState } from "@/hooks/use-visual-state";
import { useDashboardAgent } from "@/hooks/use-dashboard-agent";
import { ComponentRenderer } from "@/components/component-renderer";
import { DefaultDashboard } from "@/components/business/default-dashboard";
import { WelcomeSkeleton } from "./welcome-skeleton";
import { SrcdocRenderer } from "./srcdoc-renderer";
import { Button } from "@/components/ui/button";
import { AlertCircle, ArrowLeft, RefreshCw, Loader2 } from "lucide-react";
import { reportDashboardError } from "@/lib/dashboard/error-reporter";

export function LiveCanvas() {
  const {
    currentView,
    viewStack,
    popView,
    clearViews,
    canvasMode,
    setCanvasMode,
    dashboardCode,
    setDashboardCode,
    clearDashboard,
    isUpdating,
    pendingAgentData,
  } = useVisualState();

  const [error, setError] = useState<string | null>(null);
  // The page on screen came from an earlier session, not from this one: offer
  // to regenerate it rather than leave a stale view looking current.
  // Held as the restored html, so the offer goes away once anything replaces it.
  const [restoredHtml, setRestoredHtml] = useState<string | null>(null);
  const welcomeLoadedRef = useRef(false);
  const welcomeAbortRef = useRef<AbortController | null>(null);

  // Background dashboard agent — handles conversation-driven updates. Its
  // failures come back here so the operator sees them.
  const agent = useDashboardAgent();

  /**
   * Generate the welcome page. On first mount a page saved by an earlier
   * session is restored instead (`restore`); Retry and Regenerate skip that
   * and ask for a fresh one. Every failure lands in the error state with its
   * reason — an answer without a page included — never in a silent fallback.
   */
  const loadWelcome = useCallback(
    ({ restore }: { restore: boolean }) => {
      welcomeAbortRef.current?.abort();
      const abort = new AbortController();
      welcomeAbortRef.current = abort;
      setCanvasMode("loading");

      const restoreSaved = async (): Promise<boolean> => {
        if (!restore) return false;
        const res = await fetch("/api/session", { signal: abort.signal });
        if (!res.ok) return false;
        // Guard against non-JSON responses (e.g., Bridge returning HTML error pages)
        const text = await res.text();
        try {
          const data = JSON.parse(text);
          if (data.html) {
            setDashboardCode(data.html, "html");
            setRestoredHtml(data.html);
            return true;
          }
        } catch {
          // Session response wasn't valid JSON — fall through to welcome
        }
        return false;
      };

      const generate = async () => {
        const res = await fetch("/api/dashboard/welcome", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
          signal: abort.signal,
        });
        // Response may contain leading whitespace (keepalive padding) —
        // parse from text to be safe across all runtimes.
        const text = await res.text();
        let data: { html?: string; type?: "html" | "tsx"; error?: string };
        try {
          data = JSON.parse(text.trim());
        } catch {
          throw new Error(`The canvas service answered with something unreadable (HTTP ${res.status}).`);
        }
        if (data.error) throw new Error(data.error);
        if (!res.ok) throw new Error(`The canvas service answered HTTP ${res.status}.`);
        if (!data.html) throw new Error("The canvas service answered without a page.");
        setDashboardCode(data.html, data.type || "html");
      };

      restoreSaved()
        .then((done) => (done ? undefined : generate()))
        .catch((err) => {
          if ((err as Error).name === "AbortError") return;
          console.error("[welcome] Failed:", err);
          reportDashboardError("welcome", String(err));
          setError(err instanceof Error ? err.message : String(err));
          setCanvasMode("error");
        });
    },
    [setCanvasMode, setDashboardCode],
  );

  // Welcome dashboard on first load — try restore first, then generate
  useEffect(() => {
    if (welcomeLoadedRef.current) return;
    welcomeLoadedRef.current = true;

    // Don't auto-generate if we have a view or existing dashboard — or the
    // agent has asked for one, which the dashboard agent is about to render
    // (a welcome page landing after it would overwrite the requested visual).
    if (currentView || dashboardCode || pendingAgentData) return;

    loadWelcome({ restore: true });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    return () => welcomeAbortRef.current?.abort();
  }, []);

  // Save dashboard to session when it changes (debounced to avoid rapid-fire POSTs)
  const lastSavedRef = useRef<string | null>(null);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!dashboardCode || dashboardCode === lastSavedRef.current) return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      lastSavedRef.current = dashboardCode;
      fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ html: dashboardCode }),
      }).catch(() => {
        // Non-critical — session save is best-effort
      });
    }, 3000);
    return () => { if (saveTimerRef.current) clearTimeout(saveTimerRef.current); };
  }, [dashboardCode]);

  // Retry asks for a page again; it used to reset the canvas to idle and ask
  // for nothing.
  const handleRetry = useCallback(() => {
    setError(null);
    setRestoredHtml(null);
    clearDashboard();
    loadWelcome({ restore: false });
  }, [clearDashboard, loadWelcome]);

  const handleRegenerate = useCallback(() => {
    setError(null);
    setRestoredHtml(null);
    loadWelcome({ restore: false });
  }, [loadWelcome]);

  return (
    <div
      className="h-full w-full flex flex-col relative"
      data-testid="live-canvas"
    >
      {/* Navigation bar */}
      {viewStack.length > 0 && (
        <div className="flex items-center gap-2 p-2 border-b border-border">
          <Button
            variant="ghost"
            size="sm"
            onClick={popView}
            data-testid="back-button"
          >
            <ArrowLeft className="size-3.5" /> Back
          </Button>
          {currentView && (
            <span className="text-sm font-medium">{currentView.title}</span>
          )}
          {viewStack.length > 1 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={clearViews}
              className="ml-auto"
            >
              Dashboard
            </Button>
          )}
        </div>
      )}

      {/* Updating spinner overlay — top-right corner */}
      {isUpdating && canvasMode === "dashboard" && (
        <div
          className="absolute top-3 right-3 z-10 flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-popover/80 backdrop-blur-sm border border-border"
          data-testid="updating-spinner"
        >
          <Loader2 className="w-3 h-3 animate-spin text-primary" />
          <span className="text-xs text-muted-foreground">Updating</span>
        </div>
      )}

      {/* A conversation-driven update failed: say why, keep the current view */}
      {agent.error && (
        <div
          role="alert"
          className="flex items-center gap-2 border-b border-border bg-destructive/5 px-3 py-2"
          data-testid="canvas-update-error"
        >
          <AlertCircle className="size-3.5 shrink-0 text-destructive" />
          <span className="text-xs text-muted-foreground">{agent.error}</span>
          <Button
            variant="outline"
            size="xs"
            className="ml-auto"
            onClick={agent.retry}
            data-testid="canvas-update-retry"
          >
            <RefreshCw className="size-3" />
            Retry
          </Button>
          <Button variant="ghost" size="xs" onClick={agent.dismiss}>
            Dismiss
          </Button>
        </div>
      )}

      {/* A page restored from an earlier session can be regenerated */}
      {restoredHtml !== null &&
        dashboardCode === restoredHtml &&
        canvasMode === "dashboard" &&
        !isUpdating && (
        <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
          <span className="text-xs text-muted-foreground">
            Restored from your last session.
          </span>
          <Button
            variant="ghost"
            size="xs"
            className="ml-auto"
            onClick={handleRegenerate}
            data-testid="canvas-regenerate"
          >
            <RefreshCw className="size-3" />
            Regenerate
          </Button>
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {/* Native component mode */}
        {canvasMode === "native" && currentView && (
          <div className="p-4">
            <ComponentRenderer
              toolName={currentView.toolName}
              props={currentView.props}
            />
          </div>
        )}

        {/* Loading mode — skeleton only (no streaming code preview) */}
        {canvasMode === "loading" && (
          <WelcomeSkeleton />
        )}

        {/* Dashboard code rendered — always use srcdoc (HTML-first) */}
        {canvasMode === "dashboard" && dashboardCode && (
          <div className="h-full">
            <SrcdocRenderer html={dashboardCode} />
          </div>
        )}

        {/* Error mode */}
        {canvasMode === "error" && (
          <div className="p-6 flex flex-col items-center gap-4" role="alert" data-testid="canvas-error">
            <AlertCircle className="w-8 h-8 text-destructive" />
            <p className="text-sm text-muted-foreground text-center">
              {error || "Failed to generate dashboard"}
            </p>
            <Button variant="outline" size="sm" onClick={handleRetry} data-testid="canvas-retry">
              <RefreshCw className="w-3 h-3 mr-2" />
              Retry
            </Button>
          </div>
        )}

        {/* Idle mode — show default dashboard */}
        {canvasMode === "idle" && !currentView && (
          <div className="p-4">
            <DefaultDashboard />
          </div>
        )}
      </div>
    </div>
  );
}
