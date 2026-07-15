"use client";

import { useCallback, useEffect, useState } from "react";

const BRIDGE_URL = "/api/bridge";

type Finding = { code: string; message: string };
type Agent = {
  agent_id: string;
  name?: string;
  department?: string;
  model?: string;
  sandbox?: string;
  delivery_mode?: string;
  tools_allowed?: string[];
  exec_allowlist?: string[];
  enabled?: boolean | null;
  last_run_at?: string | null;
  last_status?: string | null;
  runs_7d?: number;
  failures_7d?: number;
  findings?: Finding[];
};

export function FleetView({ visible = true }: { visible?: boolean }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [error, setError] = useState<string | null>(null);

  const fetchFleet = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE_URL}/api/fleet`);
      if (!res.ok) {
        setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
        return;
      }
      setAgents(await res.json());
      setError(null);
    } catch {
      setError("Could not reach the bridge.");
    }
  }, []);

  useEffect(() => {
    if (visible) fetchFleet();
  }, [visible, fetchFleet]);

  return (
    <div
      data-testid="fleet-view"
      className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}
    >
      <h2 className="text-lg font-semibold text-zinc-100">Fleet</h2>
      {error && <p className="text-amber-400 text-sm">{error}</p>}
      <div className="flex flex-col gap-2">
        {agents.map((a) => {
          const flagged = (a.findings?.length ?? 0) > 0;
          return (
            <div
              key={a.agent_id}
              data-testid={`fleet-agent-${a.agent_id}`}
              data-finding={flagged ? "true" : "false"}
              className={`rounded-md border p-3 ${
                flagged ? "border-amber-500/50 bg-amber-500/5" : "border-zinc-700 bg-zinc-900"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="font-medium text-zinc-100">{a.name ?? a.agent_id}</span>
                <span className="text-xs text-zinc-400">{a.model}</span>
              </div>
              <div className="mt-1 flex flex-wrap gap-2 text-xs text-zinc-400">
                <span>sandbox: {a.sandbox ?? "—"}</span>
                <span>delivery: {a.delivery_mode ?? "—"}</span>
                <span>exec_allowlist: {(a.exec_allowlist?.length ?? 0)} rule(s)</span>
                <span>7d: {a.runs_7d ?? 0} run(s), {a.failures_7d ?? 0} fail</span>
                <span>last: {a.last_status ?? "—"}</span>
              </div>
              {flagged && (
                <ul className="mt-2 list-disc pl-5 text-xs text-amber-300">
                  {a.findings!.map((f) => (
                    <li key={f.code}>{f.message}</li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default FleetView;
