"use client";

import { useCallback, useEffect, useState } from "react";
import { Activity } from "lucide-react";
import { PageHeader } from "@/components/business/page-header";
import { EmptyState } from "@/components/business/empty-state";
import { StatusBadge, fromEngineStatus } from "@/components/business/status-badge";
import { Skeleton } from "@/components/ui/skeleton";

const BRIDGE_URL = "/api/bridge";

type Run = { id: string; agent_id?: string; status?: string; total_cost_usd?: number;
  started_at?: string | null; duration_ms?: number | null };
type Step = { step_number: number; step_type?: string; tool_name?: string; error_message?: string | null };
type GEvent = { guardrail_name?: string; action?: string; tool_name?: string; reason?: string };
type Detail = { run: Run; steps: Step[]; guardrail_events: GEvent[] };

export function RunsView({ visible = true }: { visible?: boolean }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const openRun = useCallback(async (id: string) => {
    setDetail(null);
    setError(null);
    try {
      const res = await fetch(`${BRIDGE_URL}/api/runs/${id}`);
      if (!res.ok) {
        setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
        return;
      }
      setDetail(await res.json());
    } catch {
      setError("Could not reach the bridge.");
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    (async () => {
      setLoading(true);
      try {
        const res = await fetch(`${BRIDGE_URL}/api/runs`);
        if (!active) return;
        if (!res.ok) {
          setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
          return;
        }
        const data = await res.json();
        if (!active) return;
        setRuns(data);
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
    <div data-testid="runs-view" className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}>
      <PageHeader
        title="Runs"
        description={runs.length > 0 ? `${runs.length} recent` : undefined}
      />
      {error && <p className="text-sm text-destructive">{error}</p>}

      {loading && runs.length === 0 && !error && (
        <div data-testid="runs-loading" className="flex w-full max-w-sm flex-col gap-2" aria-hidden>
          {[0, 1, 2].map((i) => (
            <div key={i} className="rounded-lg border border-border bg-card p-3">
              <Skeleton className="h-4 w-2/3" />
            </div>
          ))}
        </div>
      )}

      {!loading && !error && runs.length === 0 && (
        <EmptyState
          testId="runs-empty"
          icon={Activity}
          title="No runs yet"
          description="Runs appear here as agents execute on their schedules."
        />
      )}

      <div className="flex gap-4">
        {runs.length > 0 && (
          <div className="flex min-w-[16rem] flex-col gap-1.5">
            {runs.map((r) => (
              <button key={r.id} data-testid={`run-row-${r.id}`} onClick={() => openRun(r.id)}
                className="rounded-lg border border-border bg-card p-2.5 text-left text-sm transition-colors hover:border-ring/30 focus-visible:outline-2 focus-visible:outline-ring">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-foreground">{r.agent_id}</span>
                  <StatusBadge status={fromEngineStatus(r.status)} label={r.status ?? undefined} />
                </div>
                {typeof r.total_cost_usd === "number" && (
                  <span className="font-mono text-xs text-muted-foreground">${r.total_cost_usd.toFixed(4)}</span>
                )}
              </button>
            ))}
          </div>
        )}
        {detail && (
          <div data-testid="run-detail" className="flex-1 rounded-lg border border-border bg-card p-4 text-sm">
            <div className="flex items-center gap-3">
              <span className="font-semibold text-foreground">{detail.run.agent_id}</span>
              <StatusBadge status={fromEngineStatus(detail.run.status)} label={detail.run.status ?? undefined} />
            </div>
            <ol className="mt-3 list-decimal pl-5 font-mono text-xs leading-6 text-muted-foreground">
              {detail.steps.map((s) => (
                <li key={s.step_number}>{s.step_type}{s.tool_name ? `: ${s.tool_name}` : ""}</li>
              ))}
            </ol>
            <div className="mt-3 flex flex-col gap-1">
              {detail.guardrail_events.map((e, i) => (
                <div key={i} data-testid={`guardrail-event-${i}`}
                  className={`rounded-md px-2.5 py-1.5 text-xs ${
                    e.action === "blocked"
                      ? "border border-warning/25 bg-warning/10 text-warning"
                      : "bg-muted text-muted-foreground"
                  }`}>
                  {e.guardrail_name} {e.action} {e.tool_name}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default RunsView;
