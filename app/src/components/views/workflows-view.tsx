"use client";

import { useEffect, useState } from "react";
import { Workflow } from "lucide-react";
import { PageHeader } from "@/components/business/page-header";
import { EmptyState } from "@/components/business/empty-state";
import { StatusBadge, fromEngineStatus } from "@/components/business/status-badge";
import { Skeleton } from "@/components/ui/skeleton";

const BRIDGE_URL = "/api/bridge";

type WorkflowRow = { workflow_id: string; runs?: number; last_run_at?: string | null;
  last_status?: string | null; failures?: number };

export function WorkflowsView({ visible = true }: { visible?: boolean }) {
  const [workflows, setWorkflows] = useState<WorkflowRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    (async () => {
      setLoading(true);
      try {
        const res = await fetch(`${BRIDGE_URL}/api/workflows`);
        if (!active) return;
        if (!res.ok) {
          setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
          return;
        }
        const data = await res.json();
        if (!active) return;
        setWorkflows(data);
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
    <div data-testid="workflows-view" className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}>
      <PageHeader
        title="Workflows"
        description={workflows.length > 0 ? `${workflows.length} with run history` : undefined}
      />
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
            <div key={w.workflow_id} data-testid={`workflow-row-${w.workflow_id}`}
              className={`rounded-lg border p-3 transition-colors ${
                failed ? "border-warning/40 bg-warning/5" : "border-border bg-card hover:border-ring/25"
              }`}>
              <div className="flex items-center justify-between gap-3">
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
        <p className="mt-2 text-xs text-muted-foreground/70">
          Shows workflows that have run at least once. A defined-but-never-run workflow
          will not appear here.
        </p>
      )}
    </div>
  );
}

export default WorkflowsView;
