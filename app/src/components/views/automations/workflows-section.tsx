"use client";

import { useEffect, useState } from "react";
import { ChevronDown, Workflow } from "lucide-react";
import { EmptyState } from "@/components/business/empty-state";
import { StatusBadge, fromEngineStatus } from "@/components/business/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkflowRunDrawer } from "@/components/views/automations/workflow-run-drawer";
import { WorkflowRunsList } from "@/components/views/automations/workflow-runs";

/**
 * Workflows — the multi-agent flows, below the scheduled agents.
 *
 * Lifted out of `workflows-view.tsx` when Automations took over the
 * `?v=workflows` route. `GET /api/workflows` lists every workflow that has run
 * AND every one defined in `docs/workflows/` that has not (`runs: 0`), each
 * with a `source`: `plugin:<dist>` when a plugin shipped it, else `workspace`.
 * The source is drawn as a small label so an operator can tell a workflow they
 * wrote from one a plugin installed.
 *
 * It keeps its own fetch rather than being fed by the page above it: the two
 * halves come from two different bridge routes with two different failure
 * modes, and a workflow listing that is down must not take the automations with
 * it.
 *
 * A workflow that has run can be expanded to list its recent runs; selecting a
 * run opens the run-detail drawer with each step's input, output and error.
 */

const BRIDGE = "/api/bridge";

export type WorkflowRow = {
  workflow_id: string;
  runs?: number;
  last_run_at?: string | null;
  last_status?: string | null;
  failures?: number;
  /** `plugin:<dist>` or `workspace`; null/absent for a workflow no file defines. */
  source?: string | null;
};

export function WorkflowsSection({ visible = true }: { visible?: boolean }) {
  const [workflows, setWorkflows] = useState<WorkflowRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  // The run stays selected after the drawer closes, so its title does not
  // blank out while the sheet slides away.
  const [selected, setSelected] = useState<{ workflowId: string; runId: string } | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const toggle = (workflowId: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(workflowId)) next.delete(workflowId);
      else next.add(workflowId);
      return next;
    });

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
            ? `${workflows.length} defined or run`
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
          title="No workflows yet"
          description="A workflow appears here once it is defined in docs/workflows/ or has run."
        />
      )}

      <div className="flex flex-col gap-2">
        {workflows.map((w) => {
          const failed = (w.failures ?? 0) > 0;
          const runs = w.runs ?? 0;
          const isOpen = expanded.has(w.workflow_id);
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
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="break-all text-sm font-semibold text-foreground">
                    {w.workflow_id}
                  </span>
                  {w.source ? (
                    <span
                      data-testid={`workflow-source-${w.workflow_id}`}
                      className="break-all rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
                    >
                      {w.source}
                    </span>
                  ) : null}
                </div>
                <StatusBadge
                  status={failed ? "degraded" : fromEngineStatus(w.last_status)}
                  label={w.last_status ?? undefined}
                />
              </div>
              <div className="mt-1 flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-xs text-muted-foreground">
                  {runs === 0 ? "never run" : `${runs} run(s), ${w.failures ?? 0} failed`}
                </span>
                {runs > 0 ? (
                  <button
                    type="button"
                    data-testid={`workflow-runs-toggle-${w.workflow_id}`}
                    aria-expanded={isOpen}
                    aria-controls={`workflow-runs-${w.workflow_id}`}
                    onClick={() => toggle(w.workflow_id)}
                    className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
                  >
                    {isOpen ? "Hide runs" : "Runs"}
                    <ChevronDown
                      aria-hidden
                      className={`size-3 transition-transform ${isOpen ? "rotate-180" : ""}`}
                    />
                  </button>
                ) : null}
              </div>
              {isOpen ? (
                <WorkflowRunsList
                  workflowId={w.workflow_id}
                  onOpenRun={(runId) => {
                    setSelected({ workflowId: w.workflow_id, runId });
                    setDrawerOpen(true);
                  }}
                />
              ) : null}
            </div>
          );
        })}
      </div>

      <WorkflowRunDrawer
        workflowId={selected?.workflowId ?? ""}
        runId={selected?.runId ?? null}
        open={drawerOpen && selected !== null}
        onOpenChange={setDrawerOpen}
      />
    </section>
  );
}

export default WorkflowsSection;
