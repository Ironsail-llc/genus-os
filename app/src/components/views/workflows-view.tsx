"use client";

import { useEffect, useState } from "react";

const BRIDGE_URL = "/api/bridge";

type Workflow = { workflow_id: string; runs?: number; last_run_at?: string | null;
  last_status?: string | null; failures?: number };

export function WorkflowsView({ visible = true }: { visible?: boolean }) {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    (async () => {
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
      }
    })();
    return () => {
      active = false;
    };
  }, [visible]);

  return (
    <div data-testid="workflows-view" className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}>
      <h2 className="text-lg font-semibold text-zinc-100">Workflows</h2>
      {error && <p className="text-amber-400 text-sm">{error}</p>}
      <div className="flex flex-col gap-2">
        {workflows.map((w) => {
          const failed = (w.failures ?? 0) > 0;
          return (
            <div key={w.workflow_id} data-testid={`workflow-row-${w.workflow_id}`}
              className={`rounded-md border p-3 ${
                failed ? "border-amber-500/50 bg-amber-500/5" : "border-zinc-700 bg-zinc-900"
              }`}>
              <div className="flex items-center justify-between">
                <span className="font-medium text-zinc-100">{w.workflow_id}</span>
                <span className="text-xs text-zinc-400">{w.last_status ?? "—"}</span>
              </div>
              <div className="mt-1 text-xs text-zinc-400">
                {w.runs ?? 0} run(s), {w.failures ?? 0} failed
              </div>
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-xs text-zinc-500">
        Shows workflows that have run at least once. A defined-but-never-run workflow
        will not appear here.
      </p>
    </div>
  );
}

export default WorkflowsView;
