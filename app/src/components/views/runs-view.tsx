"use client";

import { useCallback, useEffect, useState } from "react";

const BRIDGE_URL = "/api/bridge";

type Run = { id: string; agent_id?: string; status?: string; total_cost_usd?: number;
  started_at?: string | null; duration_ms?: number | null };
type Step = { step_number: number; step_type?: string; tool_name?: string; error_message?: string | null };
type GEvent = { guardrail_name?: string; action?: string; tool_name?: string; reason?: string };
type Detail = { run: Run; steps: Step[]; guardrail_events: GEvent[] };

export function RunsView({ visible = true }: { visible?: boolean }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchRuns = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE_URL}/api/runs`);
      if (!res.ok) {
        setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
        return;
      }
      setRuns(await res.json());
      setError(null);
    } catch {
      setError("Could not reach the bridge.");
    }
  }, []);

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
    if (visible) fetchRuns();
  }, [visible, fetchRuns]);

  return (
    <div data-testid="runs-view" className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}>
      <h2 className="text-lg font-semibold text-zinc-100">Runs</h2>
      {error && <p className="text-amber-400 text-sm">{error}</p>}
      <div className="flex gap-4">
        <div className="flex flex-col gap-1 min-w-[16rem]">
          {runs.map((r) => (
            <button key={r.id} data-testid={`run-row-${r.id}`} onClick={() => openRun(r.id)}
              className="rounded border border-zinc-700 bg-zinc-900 p-2 text-left text-sm text-zinc-200 hover:border-zinc-500">
              <span className="font-medium">{r.agent_id}</span>{" "}
              <span className="text-zinc-400">{r.status}</span>
              {typeof r.total_cost_usd === "number" && (
                <span className="text-zinc-500"> · ${r.total_cost_usd.toFixed(4)}</span>
              )}
            </button>
          ))}
        </div>
        {detail && (
          <div data-testid="run-detail" className="flex-1 rounded border border-zinc-700 bg-zinc-900 p-3 text-sm">
            <div className="font-medium text-zinc-100">
              {detail.run.agent_id} — {detail.run.status}
            </div>
            <ol className="mt-2 list-decimal pl-5 text-zinc-300">
              {detail.steps.map((s) => (
                <li key={s.step_number}>{s.step_type}{s.tool_name ? `: ${s.tool_name}` : ""}</li>
              ))}
            </ol>
            <div className="mt-2 flex flex-col gap-1">
              {detail.guardrail_events.map((e, i) => (
                <div key={i} data-testid={`guardrail-event-${i}`}
                  className={`rounded px-2 py-1 text-xs ${
                    e.action === "blocked"
                      ? "bg-amber-500/10 text-amber-300"
                      : "bg-zinc-800 text-zinc-300"
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
