"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { BellRing } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";
import { StatusBadge } from "@/components/business/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

import { statusOf } from "./telemetry-logs";
import {
  TELEMETRY_BRIDGE,
  TelemetryFrame,
  TelemetryNotice,
  readTelemetry,
  records,
  str,
  timeOf,
  type TelemetryState,
} from "./telemetry-shared";

/**
 * Observe › Logs › Datadog › Monitors — the monitors inside the organization's
 * scope (`GET /api/telemetry/monitors`), firing ones only by default (alert,
 * warn, no data). A tenant whose monitors are not shared is told so in one
 * sentence, not shown an empty list.
 */

export interface MonitorRow {
  id: string;
  name: string;
  status: string;
  type: string;
  last_triggered: string | null;
}

function parseMonitor(row: Record<string, unknown>, index: number): MonitorRow {
  return {
    id: str(row.id) || `monitor-${index}`,
    name: str(row.name),
    status: str(row.status),
    type: str(row.type),
    last_triggered: str(row.last_triggered) || null,
  };
}

/** Monitor statuses on the Helm's badges: no data is a warning, not a failure. */
function monitorStatus(status: string) {
  return status.toLowerCase() === "no data" ? "degraded" : statusOf(status);
}

export interface TelemetryMonitorsProps {
  visible: boolean;
  switcher?: ReactNode;
  above?: ReactNode;
}

export function TelemetryMonitors({ visible, switcher, above }: TelemetryMonitorsProps) {
  const [firingOnly, setFiringOnly] = useState(true);
  const [reloads, setReloads] = useState(0);
  const searchKey = `${reloads}|${firingOnly}`;
  const [answered, setAnswered] = useState<{ key: string; state: TelemetryState } | null>(null);
  const [rows, setRows] = useState<MonitorRow[]>([]);
  const generation = useRef(0);
  const sentKey = useRef<string | null>(null);

  useEffect(() => {
    if (!visible || sentKey.current === searchKey) return;
    sentKey.current = searchKey;
    const mine = ++generation.current;
    void readTelemetry(`${TELEMETRY_BRIDGE}/api/telemetry/monitors?firing=${firingOnly}`).then((answer) => {
      if (generation.current !== mine) return;
      setAnswered({ key: searchKey, state: answer.state });
      setRows(records(answer.body.monitors).map(parseMonitor));
    });
  }, [visible, firingOnly, searchKey]);

  const state: TelemetryState =
    answered && answered.key === searchKey ? answered.state : { kind: "loading" };

  if (!visible) return null;

  return (
    <TelemetryFrame
      description="This organization's monitors in Datadog."
      switcher={switcher}
      above={above}
      onRefresh={() => setReloads((n) => n + 1)}
      refreshing={state.kind === "loading"}
      refreshTestId="monitors-refresh"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="monitors-firing-toggle"
          aria-pressed={firingOnly}
          onClick={() => setFiringOnly((on) => !on)}
          className={`rounded-md border px-2 py-1 text-xs transition-colors ${
            firingOnly
              ? "border-primary/25 bg-primary/10 text-foreground"
              : "border-border text-muted-foreground hover:text-foreground"
          }`}
        >
          Firing only
        </button>
        <span className="text-[11px] text-muted-foreground">
          {firingOnly ? "Alert, warn and no data." : "Every monitor of this organization."}
        </span>
      </div>

      {state.kind !== "ok" ? (
        <TelemetryNotice
          state={state}
          prefix="monitors"
          icon={BellRing}
          loadingLabel="Loading this organization's monitors"
          noun="these monitors"
        />
      ) : rows.length === 0 ? (
        <EmptyState
          testId="monitors-empty"
          icon={BellRing}
          title={firingOnly ? "Nothing is firing" : "No monitors yet"}
          description={
            firingOnly
              ? "Every monitor of this organization is OK. Turn off Firing only to see them all."
              : "This organization has no monitors in Datadog."
          }
        />
      ) : (
        <div className="min-w-0 rounded-lg border border-border">
          <Table data-testid="monitors-table" className="min-w-[560px] text-xs">
            <TableHeader className="bg-muted/50 text-[10px] uppercase tracking-wide text-muted-foreground">
              <TableRow>
                <TableHead className="h-8 text-muted-foreground">Name</TableHead>
                <TableHead className="h-8 w-28 text-muted-foreground">Status</TableHead>
                <TableHead className="h-8 w-44 text-muted-foreground">Last triggered</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.id} data-testid={`monitors-row-${row.id}`} className="align-top">
                  <TableCell className="max-w-0 py-1.5">
                    <span className="block truncate text-foreground" title={row.name}>
                      {row.name || "Untitled monitor"}
                    </span>
                    {row.type ? <span className="text-[10.5px] text-muted-foreground">{row.type}</span> : null}
                  </TableCell>
                  <TableCell className="py-1.5">
                    <StatusBadge status={monitorStatus(row.status)} label={row.status || "—"} />
                  </TableCell>
                  <TableCell className="py-1.5 tabular-nums text-muted-foreground" title={row.last_triggered ?? ""}>
                    {row.last_triggered ? timeOf(row.last_triggered) : "Never"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </TelemetryFrame>
  );
}

export default TelemetryMonitors;
