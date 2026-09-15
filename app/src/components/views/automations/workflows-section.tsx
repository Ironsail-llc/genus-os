"use client";

import { useEffect, useState } from "react";
import { Workflow } from "lucide-react";
import { EmptyState } from "@/components/business/empty-state";
import { StatusBadge, fromEngineStatus } from "@/components/business/status-badge";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * Workflows — the multi-agent flows, below the scheduled agents.
 *
 * Lifted verbatim out of `workflows-view.tsx` when Automations took over the
 * `?v=workflows` route. Unchanged in substance, including the honesty
 * limitation in the footer: `GET /api/workflows` groups `workflow_runs`, so a
 * workflow that has been DEFINED and never RUN is not in this list and the copy
 * says so rather than implying full registry coverage.
 *
 * It keeps its own fetch rather than being fed by the page above it: the two
 * halves come from two different bridge routes with two different failure
 * modes, and a workflow listing that is down must not take the automations with
 * it.
 */

const BRIDGE = "/api/bridge";

export type WorkflowRow = {
  workflow_id: string;
  runs?: number;
  last_run_at?: string | null;
  last_status?: string | null;
  failures?: number;
};

export function WorkflowsSection({ visible = true }: { visible?: boolean }) {
  const [workflows, setWorkflows] = useState<WorkflowRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    (async () => {
      setLoading(true);
      try {
        const res = await fetch(`${BRIDGE}/api/workflows`);
        if (!active) return;
        if (!res.ok) {
          setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
          return;
        }
        const data = await res.json();
        if (!active) return;
        setWorkflows(Array.isArray(data) ? data : []);
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
  }, [visible]);

  return (
    <section className="flex flex-col gap-2" data-testid="workflows-section">
      <div>
        <h3 className="text-sm font-medium text-foreground">Workflows</h3>
        <p className="text-xs text-muted-foreground">
          {workflows.length > 0
            ? `${workflows.length} with run history`
            : "Multi-agent flows, and what they have run."}
        </p>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {loading && workflows.length === 0 && !error && (
        <div data-testid="workflows-loading" className="flex flex-col gap-2" aria-hidden>
          {[0, 1].map((i) => (
            <div key={i} className="rounded-lg border border-border bg-card p-3">
              <Skeleton className="h-4 w-1/3" />
              <Skeleton className="mt-2 h-3 w-1/2" />
            </div>
          ))}
        </div>
      )}

      {!loading && !error && workflows.length === 0 && (
        <EmptyState
          testId="workflows-empty"
          icon={Workflow}
          title="No workflow runs yet"
          description="A workflow appears here after its first run — defined-but-never-run workflows are not listed."
        />
      )}

      <div className="flex flex-col gap-2">
        {workflows.map((w) => {
          const failed = (w.failures ?? 0) > 0;
          return (
            <div
              key={w.workflow_id}
              data-testid={`workflow-row-${w.workflow_id}`}
              className={`rounded-lg border p-3 transition-colors ${
                failed
                  ? "border-warning/40 bg-warning/5"
                  : "border-border bg-card hover:border-ring/25"
              }`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-semibold text-foreground">{w.workflow_id}</span>
                <StatusBadge
                  status={failed ? "degraded" : fromEngineStatus(w.last_status)}
                  label={w.last_status ?? undefined}
                />
              </div>
              <div className="mt-1 font-mono text-xs text-muted-foreground">
                {w.runs ?? 0} run(s), {w.failures ?? 0} failed
              </div>
            </div>
          );
        })}
      </div>

      {workflows.length > 0 && (
        <p className="text-xs text-muted-foreground/70">
          Shows workflows that have run at least once. A defined-but-never-run workflow will not
          appear here.
        </p>
      )}
    </section>
  );
}

export default WorkflowsSection;
