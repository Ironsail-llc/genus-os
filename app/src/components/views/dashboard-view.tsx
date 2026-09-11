"use client";

import { useState } from "react";
import { ArrowLeft, Sparkles } from "lucide-react";
import { LiveCanvas } from "@/components/canvas/live-canvas";
import { ComponentRenderer } from "@/components/component-renderer";
import { DefaultDashboard } from "@/components/business/default-dashboard";
import { Button } from "@/components/ui/button";
import { useVisualState } from "@/hooks/use-visual-state";

interface DashboardViewProps {
  visible: boolean;
}

/**
 * The operator's home.
 *
 * Home is the real-data dashboard — health, tasks, agents, quick actions —
 * fetched from the BFF. It used to be an LLM-generated page, which cost a
 * model call on every page load and, when generation returned nothing usable,
 * left the operator staring at a blank iframe.
 *
 * The generated canvas is still one click away, but it mounts only when asked
 * for, so opening the app makes no model call at all.
 */
export function DashboardView({ visible }: DashboardViewProps) {
  const [aiCanvasOpen, setAiCanvasOpen] = useState(false);
  const { currentView, viewStack, popView } = useVisualState();

  return (
    <div
      className="h-full w-full flex-col"
      style={{ display: visible ? "flex" : "none" }}
      data-testid="dashboard-view"
    >
      {aiCanvasOpen ? (
        <>
          <div className="flex items-center gap-2 border-b border-border p-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setAiCanvasOpen(false)}
              data-testid="dashboard-close-ai"
            >
              <ArrowLeft className="size-3.5" /> Dashboard
            </Button>
            <span className="text-xs text-muted-foreground">
              An AI-generated view of your data
            </span>
          </div>
          <div className="min-h-0 flex-1">
            <LiveCanvas />
          </div>
        </>
      ) : currentView && viewStack.length > 0 ? (
        <>
          <div className="flex items-center gap-2 border-b border-border p-2">
            <Button variant="ghost" size="sm" onClick={popView} data-testid="dashboard-back">
              <ArrowLeft className="size-3.5" /> Back
            </Button>
            <span className="text-sm font-medium">{currentView.title}</span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <ComponentRenderer toolName={currentView.toolName} props={currentView.props} />
          </div>
        </>
      ) : (
        <>
          <div className="flex items-center justify-end border-b border-border p-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setAiCanvasOpen(true)}
              data-testid="dashboard-generate-ai"
            >
              <Sparkles className="size-3.5" /> Generate a view with AI
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <DefaultDashboard />
          </div>
        </>
      )}
    </div>
  );
}
