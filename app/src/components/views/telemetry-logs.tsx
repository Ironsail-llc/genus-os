"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { FileText, RefreshCw, Search } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";
import { ListSkeleton } from "@/components/business/list-skeleton";
import { PageHeader } from "@/components/business/page-header";
import { StatusBadge, type Status } from "@/components/business/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { BRIDGE_UNREACHABLE } from "@/lib/bridge/use-bridge-poll";
import { isLogSince } from "@/lib/observe/since";

/**
 * Observe › Logs › Datadog — the organization's own lines in the shared
 * Datadog account (`crm/bridge/routers/telemetry.py`).
 *
 * **The bridge holds the key and picks the tenant.** The page sends a query
 * and a window, never a tenant: the bridge answers for the tenant the session
 * acts in and ANDs that tenant's scope into every query, so nothing typed here
 * can reach another organization's lines.
 *
 * **Search runs on submit.** Each search is a Datadog read and an audit row on
 * the bridge, so typing does nothing until Enter or the Search button. The time
 * range applies at once, like the filters on Runs.
 *
 * **Every answer has a place, in plain words.** Not connected, unavailable (by
 * kind, never the vendor's text), a refused query (the bridge's own reason,
 * which the person can act on), a refusal of the role, and an empty window.
 * Only a successful read may say "nothing matched".
 */

const BRIDGE = "/api/bridge";
const PAGE_SIZE = 50;

/** The windows offered, in the Observe pages' own `since` spelling (`lib/observe/since.ts`). */
export const TELEMETRY_WINDOWS: Array<{ value: string; label: string }> = [
  { value: "15m", label: "Last 15 minutes" },
  { value: "1h", label: "Last hour" },
  { value: "4h", label: "Last 4 hours" },
  { value: "24h", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
];
const DEFAULT_WINDOW = "1h";

export const NOT_CONNECTED = "Datadog is not connected for this organization yet.";

export interface TelemetryRow {
  id: string;
  timestamp: string;
  service: string;
  status: string;
  host: string;
  message: string;
  tags: string[];
  attributes: Record<string, unknown>;
}

interface Bucket {
  by: string;
  count: number;
}

type ReadState =
  | { kind: "loading" }
  | { kind: "ok" }
  | { kind: "not_configured"; message: string; reason: string | null }
  | { kind: "unavailable"; errorKind: string }
  | { kind: "refused"; reason: string }
  | { kind: "forbidden" }
  | { kind: "error"; message: string };

/** What a Datadog failure means to the person reading, by the bridge's kind. */
export function unavailableText(kind: string): string {
  switch (kind) {
    case "auth":
      return "Datadog refused this instance's key. An operator needs to check the Datadog API and application keys.";
    case "rate_limit":
      return "Datadog is getting too many requests from this instance right now. Try again in a minute.";
    case "network":
      return "Datadog could not be reached. Try again in a moment.";
    case "bad_query":
      return "Datadog could not run that search. Check the query syntax and try again.";
    default:
      return "Datadog answered in a way Genus could not read. Try again in a moment.";
  }
}

/** The root operator is told which half is missing; everyone else is not. */
function notConfiguredHint(reason: string | null): string {
  if (reason === "keys") {
    return "The Datadog API and application keys are not set on this instance.";
  }
  if (reason === "scope") {
    return "This organization has no entry in the Datadog scope map (ROBOTHOR_DATADOG_SCOPES).";
  }
  return "An operator of this instance connects it. Nothing here is broken.";
}

/** Datadog's statuses on the five the Helm draws. */
export function statusOf(status: string): Status {
  switch (status.toLowerCase()) {
    case "emergency":
    case "alert":
    case "critical":
    case "error":
      return "failing";
    case "warn":
    case "warning":
      return "degraded";
    case "ok":
    case "success":
      return "ok";
    default:
      return "idle";
  }
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function parseRows(value: unknown): TelemetryRow[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((entry): entry is Record<string, unknown> => Boolean(entry) && typeof entry === "object")
    .map((row, index) => ({
      id: str(row.id) || `row-${index}`,
      timestamp: str(row.timestamp),
      service: str(row.service),
      status: str(row.status),
      host: str(row.host),
      message: str(row.message),
      tags: Array.isArray(row.tags) ? row.tags.filter((t): t is string => typeof t === "string") : [],
      attributes:
        row.attributes && typeof row.attributes === "object" && !Array.isArray(row.attributes)
          ? (row.attributes as Record<string, unknown>)
          : {},
    }));
}

function parseBuckets(value: unknown): Bucket[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((entry): entry is Record<string, unknown> => Boolean(entry) && typeof entry === "object")
    .map((b) => ({ by: str(b.by) || "(none)", count: typeof b.count === "number" ? b.count : 0 }));
}

function timeOf(value: string): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function params(query: string, since: string, extra: Record<string, string> = {}): string {
  const out = new URLSearchParams();
  if (query) out.set("q", query);
  out.set("since", since);
  for (const [key, value] of Object.entries(extra)) out.set(key, value);
  return out.toString();
}

/** One answer of `GET /api/telemetry/logs`, as a state and (when ok) a page. */
async function readPage(
  url: string,
): Promise<{ state: ReadState; rows: TelemetryRow[]; next: string | null }> {
  let res: Response;
  try {
    res = await fetch(url);
  } catch {
    return { state: { kind: "error", message: BRIDGE_UNREACHABLE }, rows: [], next: null };
  }
  if (res.status === 403) return { state: { kind: "forbidden" }, rows: [], next: null };
  if (!res.ok) {
    const reply = await readBridgeReply(res);
    // 400 is the scope wall's refusal and 422 a window or limit the route
    // could not read: both are the person's to fix, in the bridge's words.
    const state: ReadState =
      res.status === 400 || res.status === 422
        ? { kind: "refused", reason: reply }
        : { kind: "error", message: reply };
    return { state, rows: [], next: null };
  }
  let body: Record<string, unknown> = {};
  try {
    body = ((await res.json()) ?? {}) as Record<string, unknown>;
  } catch {
    return { state: { kind: "error", message: BRIDGE_UNREACHABLE }, rows: [], next: null };
  }
  if (body.status === "not_configured") {
    return {
      state: {
        kind: "not_configured",
        message: str(body.message) || NOT_CONNECTED,
        reason: str(body.reason) || null,
      },
      rows: [],
      next: null,
    };
  }
  if (body.status === "unavailable") {
    return { state: { kind: "unavailable", errorKind: str(body.kind) }, rows: [], next: null };
  }
  return {
    state: { kind: "ok" },
    rows: parseRows(body.rows),
    next: typeof body.next_cursor === "string" && body.next_cursor ? body.next_cursor : null,
  };
}

/** The buckets of `GET /api/telemetry/logs/summary`, or null for any answer that is not `ok`. */
async function readSummary(url: string): Promise<Bucket[] | null> {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    const body = ((await res.json()) ?? {}) as Record<string, unknown>;
    return body.status === "ok" ? parseBuckets(body.buckets) : null;
  } catch {
    return null;
  }
}

export interface TelemetryLogsProps {
  visible: boolean;
  /** The source switch, when the page offers more than one source. */
  switcher?: React.ReactNode;
}

export function TelemetryLogs({ visible, switcher }: TelemetryLogsProps) {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [since, setSince] = useState(DEFAULT_WINDOW);
  const [reloads, setReloads] = useState(0);

  // The search on screen, and the one the state below answers. They differ
  // exactly while a first page is in flight, which is what "loading" means;
  // deriving it keeps the effect from setting state before its read.
  const searchKey = `${reloads}|${since}|${query}`;
  const [answered, setAnswered] = useState<{ key: string; state: ReadState } | null>(null);
  const [rows, setRows] = useState<TelemetryRow[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [summary, setSummary] = useState<{ key: string; buckets: Bucket[] | null } | null>(null);
  // Which search a "Load more" is running for. Keyed rather than a boolean so
  // a page still loading for an old search never disables the new search's
  // button: it reads as loading only while it belongs to the search on screen.
  const [moreFor, setMoreFor] = useState<string | null>(null);
  const [open, setOpen] = useState<TelemetryRow | null>(null);

  // Only the newest search may write: a slow answer to an old query must not
  // overwrite the rows for the one on screen.
  const generation = useRef(0);
  // The search last sent. Coming back on screen is not a new search: the
  // shell keeps this view mounted, so the rows are still here, and another
  // read would be another Datadog query and another audit row for nothing.
  const sentKey = useRef<string | null>(null);

  useEffect(() => {
    if (!visible || sentKey.current === searchKey) return;
    sentKey.current = searchKey;
    const mine = ++generation.current;
    void readPage(`${BRIDGE}/api/telemetry/logs?${params(query, since, { limit: String(PAGE_SIZE) })}`).then(
      (page) => {
        if (generation.current !== mine) return;
        setAnswered({ key: searchKey, state: page.state });
        setRows(page.rows);
        setNext(page.next);
      },
    );
    // Each summary read is a Datadog query and an audit row too: it follows
    // the search, it does not beat on its own.
    void readSummary(
      `${BRIDGE}/api/telemetry/logs/summary?${params(query, since, { group_by: "service" })}`,
    ).then((buckets) => {
      if (generation.current !== mine) return;
      setSummary({ key: searchKey, buckets });
    });
  }, [visible, query, since, searchKey]);

  const state: ReadState =
    answered && answered.key === searchKey ? answered.state : { kind: "loading" };
  const setState = useCallback(
    (value: ReadState) => setAnswered({ key: searchKey, state: value }),
    [searchKey],
  );

  const loadMore = useCallback(async () => {
    if (!next) return;
    const mine = generation.current;
    const key = searchKey;
    setMoreFor(key);
    const page = await readPage(
      `${BRIDGE}/api/telemetry/logs?${params(query, since, { limit: String(PAGE_SIZE), cursor: next })}`,
    );
    // Cleared before the staleness check, and only if still ours: a stale
    // page must neither land nor leave a later button stuck on "Loading…".
    setMoreFor((current) => (current === key ? null : current));
    if (generation.current !== mine) return;
    if (page.state.kind !== "ok") {
      setState(page.state);
      return;
    }
    setRows((prev) => {
      const seen = new Set(prev.map((r) => r.id));
      return [...prev, ...page.rows.filter((r) => !seen.has(r.id))];
    });
    setNext(page.next);
  }, [next, query, since, searchKey, setState]);

  const loadingMore = moreFor !== null && moreFor === searchKey;
  const buckets = summary && summary.key === searchKey ? summary.buckets : null;

  const submit = useCallback(
    (event?: React.FormEvent) => {
      event?.preventDefault();
      const nextQuery = draft.trim();
      if (nextQuery === query) {
        // Same search again is a refresh, which has to be asked for.
        setReloads((n) => n + 1);
        return;
      }
      setQuery(nextQuery);
    },
    [draft, query],
  );

  const onWindow = useCallback((value: string) => {
    if (isLogSince(value)) setSince(value);
  }, []);

  if (!visible) return null;

  const loading = state.kind === "loading";
  const showSummary = state.kind === "ok" && buckets !== null;
  const total = buckets?.reduce((sum, b) => sum + b.count, 0) ?? 0;
  const largest = buckets?.reduce((max, b) => Math.max(max, b.count), 0) ?? 0;

  return (
    <div className="flex h-full min-w-0 flex-col gap-3 overflow-y-auto p-4" data-testid="logs-view">
      <PageHeader title="Logs" description="This organization's logs in Datadog." className="flex-wrap">
        {switcher}
        <Button
          variant="outline"
          size="sm"
          data-testid="telemetry-refresh"
          onClick={() => setReloads((n) => n + 1)}
        >
          <RefreshCw aria-hidden className={loading ? "animate-spin" : undefined} />
          Refresh
        </Button>
      </PageHeader>

      <form
        data-testid="telemetry-search"
        role="search"
        onSubmit={submit}
        className="flex min-w-0 flex-wrap items-center gap-2"
      >
        <Input
          data-testid="telemetry-query"
          aria-label="Search logs"
          placeholder="Search, e.g. status:error service:shop-api"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="h-8 min-w-0 flex-1 basis-56 font-mono text-xs"
        />
        <NativeSelect
          data-testid="telemetry-since"
          aria-label="Time range"
          value={since}
          onChange={(event) => onWindow(event.target.value)}
        >
          {TELEMETRY_WINDOWS.map((w) => (
            <option key={w.value} value={w.value}>
              {w.label}
            </option>
          ))}
        </NativeSelect>
        <Button type="submit" size="sm" data-testid="telemetry-submit">
          <Search aria-hidden />
          Search
        </Button>
      </form>

      {showSummary ? (
        <section
          data-testid="telemetry-summary"
          aria-label="Errors by service"
          className="min-w-0 rounded-lg border border-border bg-card p-3"
        >
          <div className="mb-2 flex items-baseline gap-2">
            <h3 className="text-xs font-medium text-foreground">Errors by service</h3>
            <span className="text-[11px] text-muted-foreground">
              {total} {total === 1 ? "error" : "errors"}
            </span>
          </div>
          {buckets && buckets.length > 0 ? (
            <ul className="flex flex-col gap-1">
              {buckets.map((b) => (
                <li
                  key={b.by}
                  data-testid={`telemetry-bucket-${b.by}`}
                  className="grid min-w-0 grid-cols-[minmax(0,10rem)_1fr_auto] items-center gap-2 text-xs"
                >
                  <span className="truncate font-mono text-[11px] text-foreground" title={b.by}>
                    {b.by}
                  </span>
                  <span aria-hidden className="h-1.5 rounded-full bg-muted">
                    <span
                      className="block h-full rounded-full bg-destructive/60"
                      style={{ width: `${largest ? Math.max(2, (b.count / largest) * 100) : 0}%` }}
                    />
                  </span>
                  <span className="tabular-nums text-muted-foreground">{b.count}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground">No errors in this window.</p>
          )}
        </section>
      ) : null}

      {state.kind === "loading" ? (
        <ListSkeleton testId="telemetry-loading" label="Loading this organization's logs" />
      ) : state.kind === "forbidden" ? (
        <p className="text-xs text-muted-foreground" data-testid="telemetry-forbidden">
          The bridge says these logs are not yours to read. The page is for owners, admins and
          auditors of this organization.
        </p>
      ) : state.kind === "not_configured" ? (
        <EmptyState
          testId="telemetry-not-configured"
          icon={FileText}
          title={state.message}
          description={notConfiguredHint(state.reason)}
        />
      ) : state.kind === "unavailable" ? (
        <div
          data-testid="telemetry-unavailable"
          role="status"
          className="max-w-3xl rounded-lg border border-warning/30 bg-warning/10 p-3 text-xs text-warning"
        >
          <p className="font-medium">Datadog is unavailable right now.</p>
          <p className="mt-0.5">{unavailableText(state.errorKind)}</p>
        </div>
      ) : state.kind === "refused" ? (
        <p
          data-testid="telemetry-refused"
          role="alert"
          className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
        >
          That search was refused: {state.reason}
        </p>
      ) : state.kind === "error" ? (
        <p
          data-testid="telemetry-error"
          className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
        >
          {state.message}
        </p>
      ) : rows.length === 0 ? (
        <EmptyState
          testId="telemetry-empty"
          icon={FileText}
          title="Nothing in that window"
          description="No log line of this organization matched. Widen the time range or loosen the search."
        />
      ) : (
        <>
          <div className="min-w-0 rounded-lg border border-border">
            <Table data-testid="telemetry-table" className="min-w-[640px] text-xs">
              <TableHeader className="bg-muted/50 text-[10px] uppercase tracking-wide text-muted-foreground">
                <TableRow>
                  <TableHead className="h-8 w-40 text-muted-foreground">Time</TableHead>
                  <TableHead className="h-8 w-36 text-muted-foreground">Service</TableHead>
                  <TableHead className="h-8 w-24 text-muted-foreground">Status</TableHead>
                  <TableHead className="h-8 text-muted-foreground">Message</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row, index) => (
                  <TableRow
                    key={row.id}
                    data-testid={`telemetry-row-${index}`}
                    tabIndex={0}
                    role="button"
                    aria-label={`Log line from ${row.service || "an unnamed service"}`}
                    onClick={() => setOpen(row)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setOpen(row);
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
                    <TableCell className="py-1.5">
                      <StatusBadge status={statusOf(row.status)} label={row.status || "—"} />
                    </TableCell>
                    <TableCell className="max-w-0 truncate py-1.5 font-mono text-[11px] text-foreground" title={row.message}>
                      {row.message}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>

          {next ? (
            <div className="flex justify-center pb-2">
              <Button
                variant="outline"
                size="sm"
                data-testid="telemetry-more"
                disabled={loadingMore}
                onClick={() => void loadMore()}
              >
                {loadingMore ? "Loading…" : "Load more"}
              </Button>
            </div>
          ) : null}
        </>
      )}

      <p className="shrink-0 text-[10px] text-muted-foreground">
        Only this organization&rsquo;s logs are searched. Credentials, email addresses and phone
        numbers are masked before a line leaves the server, and each search is recorded in the
        audit log.
      </p>

      <Sheet open={open !== null} onOpenChange={(isOpen) => (!isOpen ? setOpen(null) : undefined)}>
        <SheetContent data-testid="telemetry-detail" className="w-full gap-0 overflow-y-auto sm:max-w-2xl">
          <SheetHeader className="pr-10">
            <SheetTitle className="flex min-w-0 flex-wrap items-center gap-2 break-words">
              <span className="font-mono text-sm">{open?.service || "Log line"}</span>
              {open ? <StatusBadge status={statusOf(open.status)} label={open.status || "—"} /> : null}
            </SheetTitle>
            <SheetDescription className="break-all font-mono text-[11px]">
              {open ? `${open.timestamp}${open.host ? ` · ${open.host}` : ""}` : ""}
            </SheetDescription>
          </SheetHeader>
          {open ? (
            <div className="flex flex-col gap-4 px-4 pb-6 text-xs">
              <section>
                <h4 className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">Message</h4>
                <p className="whitespace-pre-wrap break-words font-mono text-[11px] text-foreground">
                  {open.message || "—"}
                </p>
              </section>
              <section>
                <h4 className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">Host</h4>
                <p className="font-mono text-[11px]">{open.host || "—"}</p>
              </section>
              <section>
                <h4 className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">Tags</h4>
                {open.tags.length > 0 ? (
                  <ul className="flex flex-wrap gap-1">
                    {open.tags.map((tag) => (
                      <li
                        key={tag}
                        className="rounded border border-border bg-muted px-1.5 py-0.5 font-mono text-[10.5px]"
                      >
                        {tag}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-muted-foreground">No tags.</p>
                )}
              </section>
              <section>
                <h4 className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">Attributes</h4>
                {Object.keys(open.attributes).length > 0 ? (
                  <pre className="overflow-x-auto rounded-md border border-border bg-muted/40 p-2 font-mono text-[10.5px] leading-relaxed">
                    {JSON.stringify(open.attributes, null, 2)}
                  </pre>
                ) : (
                  <p className="text-muted-foreground">No attributes.</p>
                )}
              </section>
            </div>
          ) : null}
        </SheetContent>
      </Sheet>
    </div>
  );
}

export default TelemetryLogs;
