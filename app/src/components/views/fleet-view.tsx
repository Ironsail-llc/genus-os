"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, Network } from "lucide-react";
import { PageHeader } from "@/components/business/page-header";
import { EmptyState } from "@/components/business/empty-state";
import { StatusBadge, fromEngineStatus } from "@/components/business/status-badge";
import { Skeleton } from "@/components/ui/skeleton";

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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!visible) return;
    let active = true;
    (async () => {
      setLoading(true);
      try {
        const res = await fetch(`${BRIDGE_URL}/api/fleet`);
        if (!active) return;
        if (!res.ok) {
          setError(res.status === 403 ? "Operator access required." : `Error ${res.status}`);
          return;
        }
        const data = await res.json();
        if (!active) return;
        setAgents(data);
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

  const flaggedCount = agents.filter((a) => (a.findings?.length ?? 0) > 0).length;

  return (
    <div
      data-testid="fleet-view"
      className="flex-col gap-3 p-4"
      style={{ display: visible ? "flex" : "none" }}
    >
      <PageHeader
        title="Fleet"
        description={
          agents.length > 0
            ? `${agents.length} agent${agents.length === 1 ? "" : "s"}${flaggedCount ? ` · ${flaggedCount} flagged` : ""}`
            : undefined
        }
      />
      {error && <p className="text-sm text-destructive">{error}</p>}

      {loading && agents.length === 0 && !error && (
        <div data-testid="fleet-loading" className="flex flex-col gap-2" aria-hidden>
          {[0, 1, 2].map((i) => (
            <div key={i} className="rounded-lg border border-border bg-card p-3">
              <Skeleton className="h-4 w-1/3" />
              <Skeleton className="mt-2 h-3 w-2/3" />
            </div>
          ))}
        </div>
      )}

      {!loading && !error && agents.length === 0 && (
        <EmptyState
          testId="fleet-empty"
          icon={Network}
          title="No agents registered"
          description="Agents appear here once their manifests are loaded by the engine."
        />
      )}

      <div className="flex flex-col gap-2">
        {agents.map((a) => {
          const flagged = (a.findings?.length ?? 0) > 0;
          return (
            <div
              key={a.agent_id}
              data-testid={`fleet-agent-${a.agent_id}`}
              data-finding={flagged ? "true" : "false"}
              className={`rounded-lg border p-3 transition-colors ${
                flagged ? "border-warning/40 bg-warning/5" : "border-border bg-card hover:border-ring/25"
              }`}
            >
              <div className="flex flex-wrap items-center gap-3">
                <StatusBadge status={flagged ? "degraded" : fromEngineStatus(a.last_status)} />
                <span className="text-sm font-semibold text-foreground">{a.name ?? a.agent_id}</span>
                <span className="ml-auto font-mono text-xs text-muted-foreground">{a.model}</span>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-muted-foreground">
                <span>sandbox {a.sandbox ?? "—"}</span>
                <span>delivery {a.delivery_mode ?? "—"}</span>
                <span>allowlist {a.exec_allowlist?.length ?? 0}</span>
                <span>
                  7d {a.runs_7d ?? 0} runs · {a.failures_7d ?? 0} fail
                </span>
                <span>last {a.last_status ?? "—"}</span>
              </div>
              {flagged && (
                <div className="mt-2 flex flex-col gap-1">
                  {a.findings!.map((f) => (
                    <div
                      key={f.code}
                      className="flex items-center gap-2 rounded-md border border-warning/25 bg-warning/10 px-2.5 py-1.5 text-xs text-warning"
                    >
                      <AlertTriangle aria-hidden className="size-3.5 shrink-0" />
                      {f.message}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default FleetView;
