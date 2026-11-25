"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Waypoints } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";
import { StatusBadge } from "@/components/business/status-badge";
import { NativeSelect } from "@/components/ui/native-select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { isLogSince } from "@/lib/observe/since";

import { TELEMETRY_WINDOWS, statusOf } from "./telemetry-logs";
import {
  TELEMETRY_BRIDGE,
  TelemetryFrame,
  TelemetryNotice,
  durationOf,
  num,
  readTelemetry,
  records,
  str,
  timeOf,
  type TelemetryState,
} from "./telemetry-shared";

/**
 * Observe › Logs › Datadog › Traces — the organization's error traces for the
 * time range (`GET /api/telemetry/traces`, errors by default), and one trace
 * as an indented tree of its spans (`GET /api/telemetry/traces/{id}`) in the
 * same sheet the log lines open in.
 *
 * A trace outside the organization's scope reads as not found, exactly like a
 * trace that does not exist: the bridge says so in one sentence, and this tab
 * shows that sentence.
 */

const PAGE_SIZE = 50;
const DEFAULT_WINDOW = "1h";

export interface TraceRow {
  trace_id: string;
  span_id: string;
  service: string;
  resource: string;
  operation: string;
  timestamp: string;
  duration_ns: number | null;
  status: string;
}

export interface TraceSpan extends TraceRow {
  parent_id: string;
  depth: number;
}

function parseRow(row: Record<string, unknown>): TraceRow {
  return {
    trace_id: str(row.trace_id),
    span_id: str(row.span_id),
    service: str(row.service),
    resource: str(row.resource),
    operation: str(row.operation),
    timestamp: str(row.timestamp),
    duration_ns: num(row.duration_ns),
    status: str(row.status),
  };
}

/**
 * The spans as a tree, depth first, each with its depth. A span whose parent
 * is not in the trace (the root's `0`, or a parent outside the window) is a
 * root. Siblings keep the bridge's order, which is by start time.
 */
export function spanTree(spans: Array<TraceRow & { parent_id: string }>): TraceSpan[] {
  const ids = new Set(spans.map((s) => s.span_id));
  const children = new Map<string, Array<TraceRow & { parent_id: string }>>();
  const roots: Array<TraceRow & { parent_id: string }> = [];
  for (const span of spans) {
    if (span.parent_id && span.parent_id !== span.span_id && ids.has(span.parent_id)) {
      const list = children.get(span.parent_id) ?? [];
      list.push(span);
      children.set(span.parent_id, list);
    } else {
      roots.push(span);
    }
  }
  const out: TraceSpan[] = [];
  const seen = new Set<string>();
  const walk = (span: TraceRow & { parent_id: string }, depth: number) => {
    if (seen.has(span.span_id)) return;
    seen.add(span.span_id);
    out.push({ ...span, depth });
    for (const child of children.get(span.span_id) ?? []) walk(child, depth + 1);
  };
  for (const root of roots) walk(root, 0);
  // A cycle has no root: show what is left flat rather than drop it.
  for (const span of spans) if (!seen.has(span.span_id)) walk(span, 0);
  return out;
}

/** A request to open one trace; `n` makes a second request for the same id a new one. */
export interface TraceRequest {
  traceId: string;
  n: number;
}

export interface TelemetryTracesProps {
  visible: boolean;
  switcher?: ReactNode;
  above?: ReactNode;
  /** A trace to open, from a log line's "View trace". */
  request?: TraceRequest | null;
}

export function TelemetryTraces({ visible, switcher, above, request }: TelemetryTracesProps) {
  const [since, setSince] = useState(DEFAULT_WINDOW);
  const [reloads, setReloads] = useState(0);
  const searchKey = `${reloads}|${since}`;
  const [answered, setAnswered] = useState<{ key: string; state: TelemetryState } | null>(null);
  const [rows, setRows] = useState<TraceRow[]>([]);
  const [open, setOpen] = useState<string | null>(null);

  // A trace asked for from another tab opens here, once per request.
  const [seenRequest, setSeenRequest] = useState<TraceRequest | null>(null);
  if (request && request !== seenRequest) {
    setSeenRequest(request);
    setOpen(request.traceId);
  }

  const generation = useRef(0);
  // Coming back on screen is not a new search (see telemetry-logs.tsx).
  const sentKey = useRef<string | null>(null);

  useEffect(() => {
    if (!visible || sentKey.current === searchKey) return;
    sentKey.current = searchKey;
    const mine = ++generation.current;
    const params = new URLSearchParams({ q: "status:error", since, limit: String(PAGE_SIZE) });
    void readTelemetry(`${TELEMETRY_BRIDGE}/api/telemetry/traces?${params}`).then((answer) => {
      if (generation.current !== mine) return;
      setAnswered({ key: searchKey, state: answer.state });
      setRows(records(answer.body.rows).map(parseRow));
    });
  }, [visible, since, searchKey]);

  const state: TelemetryState =
    answered && answered.key === searchKey ? answered.state : { kind: "loading" };

  if (!visible) return null;

  return (
    <TelemetryFrame
      description="This organization's error traces in Datadog."
      switcher={switcher}
      above={above}
      onRefresh={() => setReloads((n) => n + 1)}
      refreshing={state.kind === "loading"}
      refreshTestId="traces-refresh"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <NativeSelect
          data-testid="traces-since"
          aria-label="Time range"
          value={since}
          onChange={(event) => {
            if (isLogSince(event.target.value)) setSince(event.target.value);
          }}
        >
          {TELEMETRY_WINDOWS.map((w) => (
            <option key={w.value} value={w.value}>
              {w.label}
            </option>
          ))}
        </NativeSelect>
      </div>

      {state.kind !== "ok" ? (
        <TelemetryNotice
          state={state}
          prefix="traces"
          icon={Waypoints}
          loadingLabel="Loading this organization's error traces"
          noun="these traces"
        />
      ) : rows.length === 0 ? (
        <EmptyState
          testId="traces-empty"
          icon={Waypoints}
          title="No error trace in that window"
          description="No span of this organization ended in an error. Widen the time range to look further back."
        />
      ) : (
        <div className="min-w-0 rounded-lg border border-border">
          <Table data-testid="traces-table" className="min-w-[640px] text-xs">
            <TableHeader className="bg-muted/50 text-[10px] uppercase tracking-wide text-muted-foreground">
              <TableRow>
                <TableHead className="h-8 w-40 text-muted-foreground">Time</TableHead>
                <TableHead className="h-8 w-36 text-muted-foreground">Service</TableHead>
                <TableHead className="h-8 text-muted-foreground">Resource</TableHead>
                <TableHead className="h-8 w-24 text-right text-muted-foreground">Duration</TableHead>
                <TableHead className="h-8 w-24 text-muted-foreground">Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row, index) => (
                <TableRow
                  key={`${row.trace_id}-${row.span_id}-${index}`}
                  data-testid={`traces-row-${index}`}
                  tabIndex={0}
                  role="button"
                  aria-label={`Trace of ${row.resource || "an unnamed resource"}`}
                  onClick={() => setOpen(row.trace_id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setOpen(row.trace_id);
                    }
                  }}
                  className="cursor-pointer align-top focus-visible:outline-2 focus-visible:outline-ring"
                >
                  <TableCell className="py-1.5 tabular-nums text-muted-foreground" title={row.timestamp}>
                    {timeOf(row.timestamp)}
                  </TableCell>
                  <TableCell className="max-w-[9rem] truncate py-1.5 font-mono text-[11px]" title={row.service}>
                    {row.service || "—"}
                  </TableCell>
                  <TableCell className="max-w-0 truncate py-1.5 font-mono text-[11px] text-foreground" title={row.resource}>
                    {row.resource || row.operation || "—"}
                  </TableCell>
                  <TableCell className="py-1.5 text-right tabular-nums text-muted-foreground">
                    {durationOf(row.duration_ns)}
                  </TableCell>
                  <TableCell className="py-1.5">
                    <StatusBadge status={statusOf(row.status)} label={row.status || "—"} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <p className="shrink-0 text-[10px] text-muted-foreground">
        Only this organization&rsquo;s traces are searched, and each read is recorded in the audit log.
      </p>

      <TraceSheet traceId={visible ? open : null} onClose={() => setOpen(null)} />
    </TelemetryFrame>
  );
}

/** One trace, read when it is opened, keyed by id so a new one never shows the last. */
function TraceSheet({ traceId, onClose }: { traceId: string | null; onClose: () => void }) {
  const [loaded, setLoaded] = useState<{ id: string; state: TelemetryState; spans: TraceSpan[] } | null>(
    null,
  );
  const current = loaded && loaded.id === traceId ? loaded : null;

  useEffect(() => {
    // A trace already on screen is not read again when its tab comes back;
    // one that failed is, since reopening it is asking again.
    if (!traceId || (loaded && loaded.id === traceId && loaded.state.kind === "ok")) return;
    let active = true;
    void readTelemetry(`${TELEMETRY_BRIDGE}/api/telemetry/traces/${encodeURIComponent(traceId)}`).then(
      (answer) => {
        if (!active) return;
        const spans = records(answer.body.spans).map((s) => ({ ...parseRow(s), parent_id: str(s.parent_id) }));
        setLoaded({ id: traceId, state: answer.state, spans: spanTree(spans) });
      },
    );
    return () => {
      active = false;
    };
    // `loaded` is read to skip a trace already on screen, not to re-run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [traceId]);

  const state: TelemetryState = current ? current.state : { kind: "loading" };
  const spans = current?.spans ?? [];
  const root = spans[0];
  const longest = spans.reduce((max, s) => Math.max(max, s.duration_ns ?? 0), 0);

  return (
    <Sheet open={traceId !== null} onOpenChange={(isOpen) => (!isOpen ? onClose() : undefined)}>
      <SheetContent data-testid="trace-detail" className="w-full gap-0 overflow-y-auto sm:max-w-2xl">
        <SheetHeader className="pr-10">
          <SheetTitle className="flex min-w-0 flex-wrap items-center gap-2 break-words">
            <span className="font-mono text-sm">{root?.resource || "Trace"}</span>
            {root ? <StatusBadge status={statusOf(root.status)} label={root.status || "—"} /> : null}
          </SheetTitle>
          <SheetDescription className="break-all font-mono text-[11px]">
            {traceId ? `trace ${traceId}${spans.length ? ` · ${spans.length} spans` : ""}` : ""}
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-2 px-4 pb-6 text-xs">
          {state.kind !== "ok" ? (
            <TelemetryNotice
              state={state}
              prefix="trace"
              icon={Waypoints}
              loadingLabel="Loading the trace"
              noun="these traces"
            />
          ) : (
            <ol className="flex flex-col gap-0.5" aria-label="Spans">
              {spans.map((span) => (
                <li
                  key={span.span_id}
                  data-testid={`trace-span-${span.span_id}`}
                  data-depth={span.depth}
                  className="grid min-w-0 grid-cols-[minmax(0,1fr)_5rem_4.5rem] items-center gap-2 rounded px-1 py-1 hover:bg-muted/50"
                >
                  <span className="flex min-w-0 items-center gap-1.5" style={{ paddingLeft: `${span.depth * 14}px` }}>
                    <span
                      aria-hidden
                      className={`size-1.5 shrink-0 rounded-full ${
                        statusOf(span.status) === "failing" ? "bg-destructive" : "bg-muted-foreground/50"
                      }`}
                    />
                    <span className="truncate font-mono text-[11px] text-foreground" title={span.resource}>
                      {span.resource || span.operation || "—"}
                    </span>
                    <span className="shrink-0 truncate text-[10.5px] text-muted-foreground">{span.service}</span>
                  </span>
                  <span aria-hidden className="h-1.5 rounded-full bg-muted">
                    <span
                      className={`block h-full rounded-full ${
                        statusOf(span.status) === "failing" ? "bg-destructive/60" : "bg-primary/50"
                      }`}
                      style={{
                        width: `${longest ? Math.max(2, ((span.duration_ns ?? 0) / longest) * 100) : 0}%`,
                      }}
                    />
                  </span>
                  <span className="text-right tabular-nums text-muted-foreground">
                    {durationOf(span.duration_ns)}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

export default TelemetryTraces;
