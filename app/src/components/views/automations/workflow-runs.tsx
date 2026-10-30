"use client";

import { useEffect, useState } from "react";
import { History } from "lucide-react";
import { EmptyState } from "@/components/business/empty-state";
import { StatusBadge, fromEngineStatus } from "@/components/business/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { durationText } from "@/lib/automations/run-truth";
import { runsUrl, stepTally, type WorkflowRun } from "@/lib/automations/workflow-runs";
import { absoluteTime, relativeTime } from "@/lib/inbox/pending";

/**
 * One workflow's recent runs, drawn under its row in the Workflows section.
 *
 * Each run is a button: selecting it hands its id to the caller, which opens
 * the run-detail drawer. Fetched when mounted, which is when the operator
 * expands the workflow, so a collapsed listing costs no request per workflow.
 */
export function WorkflowRunsList({
  workflowId,
  onOpenRun,
}: {
  workflowId: string;
  onOpenRun: (runId: string) => void;
}) {
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    (async () => {
      setLoading(true);
      try {
        const res = await fetch(runsUrl(workflowId));
        if (!active) return;
        if (!res.ok) {
          setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
          return;
        }
        const data = await res.json();
        if (!active) return;
        setRuns(Array.isArray(data) ? data : []);
        setError(null);
      } catch {
        if (active) setError("Could not reach the bridge.");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [workflowId]);

  return (
    <div data-testid={`workflow-runs-${workflowId}`} className="mt-2 flex flex-col gap-1.5">
      {error && (
        <p data-testid="workflow-runs-error" className="text-xs text-destructive">
          {error}
        </p>
      )}

      {loading && !error && (
        <div data-testid="workflow-runs-loading" className="flex flex-col gap-1.5" aria-hidden>
          {[0, 1].map((i) => (
            <Skeleton key={i} className="h-9 w-full" />
          ))}
        </div>
      )}

      {!loading && !error && runs.length === 0 && (
        <EmptyState
          testId="workflow-runs-empty"
          icon={History}
          title="No runs recorded"
          description="Runs appear here once this workflow has executed."
        />
      )}

      {!loading &&
        runs.map((r) => {
          const tally = stepTally(r);
          const duration = durationText(r.duration_ms);
          return (
            <button
              key={r.id}
              type="button"
              data-testid={`workflow-run-${r.id}`}
              onClick={() => onOpenRun(r.id)}
              className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border bg-background px-2.5 py-2 text-left text-xs transition-colors hover:border-ring/30 focus-visible:outline-2 focus-visible:outline-ring"
            >
              <StatusBadge status={fromEngineStatus(r.status)} label={r.status ?? undefined} />
              {r.started_at ? (
                <time
                  data-testid={`workflow-run-started-${r.id}`}
                  dateTime={r.started_at}
                  title={absoluteTime(r.started_at)}
                  className="text-muted-foreground"
                >
                  {relativeTime(r.started_at)}
                </time>
              ) : null}
              {duration ? <span className="font-mono text-muted-foreground">{duration}</span> : null}
              {tally ? (
                <span
                  data-testid={`workflow-run-steps-${r.id}`}
                  className={`font-mono ${(r.steps_failed ?? 0) > 0 ? "text-destructive" : "text-muted-foreground"}`}
                >
                  {tally}
                </span>
              ) : null}
            </button>
          );
        })}
    </div>
  );
}
