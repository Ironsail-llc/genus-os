"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import { useVisualState } from "@/hooks/use-visual-state";
import { useDashboardAgent } from "@/hooks/use-dashboard-agent";
import { ComponentRenderer } from "@/components/component-renderer";
import { DefaultDashboard } from "@/components/business/default-dashboard";
import { WelcomeSkeleton } from "./welcome-skeleton";
import { SrcdocRenderer } from "./srcdoc-renderer";
import { Button } from "@/components/ui/button";
import { AlertCircle, RefreshCw, Loader2 } from "lucide-react";

export function LiveCanvas() {
  const {
    currentView,
    viewStack,
    popView,
    clearViews,
    canvasMode,
    setCanvasMode,
    dashboardCode,
    dashboardCodeType,
    setDashboardCode,
    clearDashboard,
    isUpdating,
    submitAction,
    resolveAction,
  } = useVisualState();

  const [error, setError] = useState<string | null>(null);
  const [welcomeLoaded, setWelcomeLoaded] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement>(null);

  // Background dashboard agent — handles conversation-driven updates
  useDashboardAgent();

  // Welcome dashboard on first load — try restore first, then generate
  useEffect(() => {
    if (welcomeLoaded) return;
    setWelcomeLoaded(true);

    // Don't auto-generate if we have a view or existing dashboard
    if (currentView || dashboardCode) return;

    const abort = new AbortController();
    setCanvasMode("loading");

    // Try to restore saved session first
    fetch("/api/session", { signal: abort.signal })
      .then(async (res) => {
        if (res.ok) {
          const data = await res.json();
          if (data.html) {
            setDashboardCode(data.html, "html");
            return; // Restored — skip welcome generation
          }
        }
        // No saved session — generate welcome dashboard
        return fetch("/api/dashboard/welcome", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
          signal: abort.signal,
        }).then(async (res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const data = await res.json();
          if (data.html) {
            setDashboardCode(data.html, data.type || "html");
          } else {
            setCanvasMode("idle");
          }
        });
      })
      .catch((err) => {
        if ((err as Error).name !== "AbortError") {
          setCanvasMode("idle");
        }
      });

    return () => {
      abort.abort();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Save dashboard to session when it changes
  const lastSavedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!dashboardCode || dashboardCode === lastSavedRef.current) return;
    lastSavedRef.current = dashboardCode;
    // Fire-and-forget save
    fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ html: dashboardCode }),
    }).catch(() => {
      // Non-critical — session save is best-effort
    });
  }, [dashboardCode]);

  // Handle actions from dashboard iframes
  const handleAction = useCallback(
    async (action: { tool: string; params: Record<string, unknown>; id: string }) => {
      submitAction({ ...action });
      try {
        const res = await fetch("/api/actions/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tool: action.tool, params: action.params }),
        });
        const data = await res.json();
        const result = {
          id: action.id,
          success: res.ok && data.success,
          data: data.data,
          error: data.error,
        };
        resolveAction(result);
        // Send result back to iframe
        iframeRef.current?.querySelector("iframe")?.contentWindow?.postMessage(
          { type: "robothor:action-result", ...result },
          "*"
        );
      } catch (err) {
        const errorMsg = err instanceof Error ? err.message : "Action failed";
        const result = { id: action.id, success: false, error: errorMsg };
        resolveAction(result);
      }
    },
    [submitAction, resolveAction]
  );

  const handleRetry = useCallback(() => {
    setError(null);
    clearDashboard();
    setCanvasMode("idle");
  }, [clearDashboard, setCanvasMode]);

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
            &larr; Back
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
          className="absolute top-3 right-3 z-10 flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-zinc-900/80 backdrop-blur-sm border border-zinc-700/50"
          data-testid="updating-spinner"
        >
          <Loader2 className="w-3 h-3 animate-spin text-indigo-400" />
          <span className="text-xs text-zinc-400">Updating</span>
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
          <div className="h-full" ref={iframeRef}>
            <SrcdocRenderer html={dashboardCode} onAction={handleAction} />
          </div>
        )}

        {/* Error mode */}
        {canvasMode === "error" && (
          <div className="p-6 flex flex-col items-center gap-4" data-testid="canvas-error">
            <AlertCircle className="w-8 h-8 text-destructive" />
            <p className="text-sm text-muted-foreground text-center">
              {error || "Failed to generate dashboard"}
            </p>
            <Button variant="outline" size="sm" onClick={handleRetry}>
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
