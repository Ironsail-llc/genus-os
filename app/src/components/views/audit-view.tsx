"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Download, Loader2, RefreshCw, ScrollText } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";
import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { isAuditReaderRole } from "@/components/layout/nav-config";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { BRIDGE_UNREACHABLE } from "@/lib/bridge/use-bridge-poll";
import { isIsoTimestamp } from "@/lib/observe/since";

/**
 * Observe › Audit — what was done, and who widened which guardrail.
 *
 * Two tabs over two routes that look alike and behave differently, which is
 * most of the reason this file is as long as it is.
 *
 * **Events** (`GET /api/audit/events`). No cursor: a `limit` up to 500 and
 * nothing else, so "Load more" is limit growth and the only signal that more
 * may exist is a FULL page. And a failure on this route is a **200 carrying an
 * `error` key**, not a 500 — that is pinned by a test on the bridge and is not
 * going to change, so the body is read before an empty list is believed.
 * Rendering "no events recorded" over a failed query would be the page making
 * a claim the server explicitly did not.
 *
 * **Flag changes** (`GET /api/controls/audit`). A keyset on `id DESC` with a
 * real `next_cursor`. Every text field on it is redacted and sanitised on the
 * bridge before it is served — `reason` is unbounded operator-typed free text,
 * and "rotating after OPENROUTER_API_KEY=… leaked" is exactly what somebody
 * types while rotating a leaked credential.
 *
 * **The export is a link, not a fetch.** `<a href download>` straight at
 * `/api/audit/events.csv` through the app's proxy: 5,000 rows pulled into a JS
 * string and handed back out as a blob is a copy in memory for no gain, and
 * the browser already knows what to do with an attachment. It carries the same
 * filters the table is showing, because an export that quietly ignored them is
 * worse than no export at all.
 *
 * **The export is appliance-wide.** `audit_log` has no tenant column — tenant
 * is a key inside `details` — so these rows are every tenant's, exactly as
 * `GET /api/audit/events` has always been for the auditor role. The tenant in
 * the filename names who exported it, not what is inside, and the note under
 * the button says so.
 *
 * `auditor` reads both of these and nothing else on `/api/controls`; Memory and
 * Logs stay operator-only. UX gating here, authorization on the bridge.
 */

const BRIDGE = "/api/bridge";

const EVENT_PAGE = 50;
/** `GET /api/audit/events` caps `limit` here. */
const EVENT_MAX = 500;
const CHANGE_PAGE = 50;

type Tab = "events" | "flags";

interface AuditEvent {
  id: string;
  timestamp: string | null;
  eventType: string;
  category: string | null;
  actor: string | null;
  action: string | null;
  target: string | null;
  status: string | null;
  sourceChannel: string | null;
  sessionKey: string | null;
  userId: string | null;
  details: string | null;
}

interface FlagChange {
  id: number;
  flag: string;
  oldValue: string | null;
  newValue: string | null;
  changedBy: string | null;
  reason: string | null;
  changedAt: string | null;
}

interface Filters {
  eventType: string;
  actor: string;
  userId: string;
  since: string;
  until: string;
}

const NO_FILTERS: Filters = { eventType: "", actor: "", userId: "", since: "", until: "" };

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function normalizeEvent(entry: unknown, index: number): AuditEvent | null {
  if (!entry || typeof entry !== "object") return null;
  const row = entry as Record<string, unknown>;
  const id =
    typeof row.id === "string" || typeof row.id === "number" ? String(row.id) : `row-${index}`;
  return {
    id,
    timestamp: str(row.timestamp),
    eventType: typeof row.event_type === "string" ? row.event_type : "(no type)",
    category: str(row.category),
    actor: str(row.actor),
    action: str(row.action),
    target: str(row.target),
    status: str(row.status),
    sourceChannel: str(row.source_channel),
    sessionKey: str(row.session_key),
    userId: str(row.user_id),
    details:
      typeof row.details === "string"
        ? row.details
        : row.details
          ? JSON.stringify(row.details)
          : null,
  };
}

function normalizeChange(entry: unknown): FlagChange | null {
  if (!entry || typeof entry !== "object") return null;
  const row = entry as Record<string, unknown>;
  if (typeof row.id !== "number") return null;
  return {
    id: row.id,
    flag: typeof row.flag === "string" ? row.flag : "(unnamed flag)",
    // NULL stays null all the way here: the bridge takes care not to turn it
    // into the word "None", and this must not turn it into "null".
    oldValue: str(row.old_value),
    newValue: str(row.new_value),
    changedBy: str(row.changed_by),
    reason: str(row.reason),
    changedAt: str(row.changed_at),
  };
}

function when(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function statusClass(status: string | null): string {
  const value = (status ?? "").toLowerCase();
  if (value === "success" || value === "allowed") return "text-success";
  if (value === "blocked" || value === "denied" || value === "failed") return "text-destructive";
  return "text-muted-foreground";
}

/** The filters, as both the table request and the export link spell them. */
function filterParams(filters: Filters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.eventType) params.set("event_type", filters.eventType);
  if (filters.actor) params.set("actor", filters.actor);
  if (filters.userId) params.set("user_id", filters.userId);
  if (filters.since) params.set("since", filters.since);
  if (filters.until) params.set("until", filters.until);
  return params;
}

export interface AuditViewProps {
  /** The shell keeps every view mounted; this is the load gate. */
  visible?: boolean;
  /** Session role. UX only — the bridge gates both routes itself. */
  role?: string | null;
  /** The session has not resolved yet, so the role is unknown — not "denied". */
  roleLoading?: boolean;
}

export function AuditView({ visible = true, role, roleLoading = false }: AuditViewProps) {
  const [tab, setTab] = useState<Tab>("events");

  const [draft, setDraft] = useState<Filters>(NO_FILTERS);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [timeError, setTimeError] = useState<{ field: "since" | "until"; message: string } | null>(
    null
  );

  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [eventLimit, setEventLimit] = useState(EVENT_PAGE);
  const [eventsLoading, setEventsLoading] = useState(true);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [pageWasFull, setPageWasFull] = useState(false);

  const [flagDraft, setFlagDraft] = useState("");
  const [flag, setFlag] = useState("");
  const [changes, setChanges] = useState<FlagChange[]>([]);
  const [changeCursor, setChangeCursor] = useState<string | null>(null);
  const [changesLoading, setChangesLoading] = useState(true);
  const [changesError, setChangesError] = useState<string | null>(null);

  /**
   * The bridge says these records are not this caller's. Not a failure — the
   * rule `use-bridge-poll`'s header states for the whole Helm, which this view
   * has to keep even though it hand-rolls its fetches. Reachable without any
   * bridge change: `require_audit_reader` also demands the platform tenant and
   * a human session, neither of which the session role in this browser knows
   * anything about.
   */
  const [forbidden, setForbidden] = useState(false);

  const mayRead = isAuditReaderRole(role) || roleLoading;
  const reading = isAuditReaderRole(role);

  const loadEvents = useCallback(
    async (limit: number) => {
      setEventsLoading(true);
      try {
        const params = filterParams(filters);
        params.set("limit", String(limit));
        const res = await fetch(`${BRIDGE}/api/audit/events?${params.toString()}`);
        if (!res.ok) {
          if (res.status === 403) {
            setForbidden(true);
            setEventsError(null);
            setEvents([]);
            return;
          }
          setEventsError(await readBridgeReply(res));
          return;
        }
        setForbidden(false);
        const body = (await res.json()) as Record<string, unknown>;
        // The route swallows a query failure into a 200. Read the body, not
        // the status: an empty list beside an `error` key is not "no events".
        if (typeof body.error === "string" && body.error) {
          setEvents([]);
          setPageWasFull(false);
          setEventsError(body.error);
          return;
        }
        const rows = Array.isArray(body.events)
          ? body.events
              .map((entry, index) => normalizeEvent(entry, index))
              .filter((e): e is AuditEvent => e !== null)
          : [];
        setEvents(rows);
        setPageWasFull(rows.length >= limit);
        setEventsError(null);
      } catch {
        setEventsError(BRIDGE_UNREACHABLE);
      } finally {
        setEventsLoading(false);
      }
    },
    [filters]
  );

  const loadChanges = useCallback(
    async (cursor?: string) => {
      setChangesLoading(true);
      try {
        const params = new URLSearchParams();
        if (flag) params.set("flag", flag);
        params.set("limit", String(CHANGE_PAGE));
        if (cursor) params.set("cursor", cursor);
        const res = await fetch(`${BRIDGE}/api/controls/audit?${params.toString()}`);
        if (!res.ok) {
          if (res.status === 403) {
            setForbidden(true);
            setChangesError(null);
            setChanges([]);
            return;
          }
          setChangesError(await readBridgeReply(res));
          return;
        }
        setForbidden(false);
        const body = (await res.json()) as Record<string, unknown>;
        const rows = Array.isArray(body.changes)
          ? body.changes.map(normalizeChange).filter((c): c is FlagChange => c !== null)
          : [];
        setChanges((previous) => (cursor ? [...previous, ...rows] : rows));
        setChangeCursor(typeof body.next_cursor === "string" ? body.next_cursor : null);
        setChangesError(null);
      } catch {
        setChangesError(BRIDGE_UNREACHABLE);
      } finally {
        setChangesLoading(false);
      }
    },
    [flag]
  );

  // Per tab, not per view: an auditor who never opens Flag changes should not
  // be putting a query on `feature_flag_audit` on every Helm page load.
  useEffect(() => {
    if (!visible || !reading || tab !== "events") return;
    void loadEvents(eventLimit);
  }, [visible, reading, tab, eventLimit, loadEvents]);

  useEffect(() => {
    if (!visible || !reading || tab !== "flags") return;
    void loadChanges();
  }, [visible, reading, tab, loadChanges]);

  const applyFilters = useCallback(() => {
    for (const field of ["since", "until"] as const) {
      const value = draft[field];
      if (value && !isIsoTimestamp(value)) {
        // Caught here so a typo is a line under the field rather than a round
        // trip — and so the export link is never built from a value the bridge
        // is about to refuse with a 422.
        setTimeError({ field, message: `${field} must be an ISO-8601 date or timestamp` });
        return;
      }
    }
    setTimeError(null);
    setEventLimit(EVENT_PAGE);
    setFilters(draft);
  }, [draft]);

  const exportHref = useMemo(() => {
    const params = filterParams(filters);
    return `${BRIDGE}/api/audit/events.csv${params.size ? `?${params.toString()}` : ""}`;
  }, [filters]);

  if (!visible) return null;

  if (!mayRead) {
    return (
      <div className="flex h-full flex-col gap-3 overflow-y-auto p-4" data-testid="audit-view">
        <PageHeader title="Audit" />
        <EmptyState
          testId="audit-not-yours"
          icon={ScrollText}
          title="The audit trail is not yours to read"
          description="Reading what was done on this instance belongs to an owner, an admin, or the read-only auditor role. The bridge refuses these routes to everybody else."
        />
      </div>
    );
  }

  return (
    <div className="flex h-full min-w-0 flex-col gap-3 overflow-y-auto p-4" data-testid="audit-view">
      <PageHeader title="Audit" description="What was done, and who changed the rules.">
        <Button
          variant="outline"
          size="sm"
          data-testid="audit-refresh"
          onClick={() => (tab === "events" ? void loadEvents(eventLimit) : void loadChanges())}
        >
          <RefreshCw
            aria-hidden
            className={
              (tab === "events" ? eventsLoading : changesLoading) ? "animate-spin" : undefined
            }
          />
          Refresh
        </Button>
      </PageHeader>

      <div
        role="tablist"
        aria-label="Audit records"
        className="flex shrink-0 items-center gap-1 self-start rounded-md border border-border p-0.5"
      >
        {(
          [
            ["events", "Events"],
            ["flags", "Flag changes"],
          ] as Array<[Tab, string]>
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            data-testid={`audit-tab-${id}`}
            onClick={() => setTab(id)}
            className={`rounded px-2.5 py-1 text-xs transition-colors ${
              tab === id
                ? "bg-primary/15 text-foreground"
                : "text-muted-foreground hover:bg-accent/60"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {forbidden ? (
        <p className="text-xs text-muted-foreground" data-testid="audit-forbidden">
          The audit records are not this session&rsquo;s to read, so there is nothing to show here. A
          role that looks like an operator or an auditor in this browser can still be refused by the
          bridge — it also requires the platform tenant and a human session. Ask an owner or admin
          on this instance.
        </p>
      ) : null}

      {tab === "events" && !forbidden ? (
        <div className="flex min-w-0 flex-col gap-3" data-testid="audit-events">
          <div className="flex min-w-0 flex-wrap items-start gap-2">
            <Input
              data-testid="audit-filter-event-type"
              aria-label="Event type"
              placeholder="Event type"
              value={draft.eventType}
              onChange={(event) => setDraft({ ...draft, eventType: event.target.value })}
              className="h-8 min-w-[130px] max-w-[200px] text-sm"
            />
            <Input
              data-testid="audit-filter-actor"
              aria-label="Actor"
              placeholder="Actor"
              value={draft.actor}
              onChange={(event) => setDraft({ ...draft, actor: event.target.value })}
              className="h-8 min-w-[110px] max-w-[180px] text-sm"
            />
            <Input
              data-testid="audit-filter-user"
              aria-label="User"
              placeholder="User"
              value={draft.userId}
              onChange={(event) => setDraft({ ...draft, userId: event.target.value })}
              className="h-8 min-w-[90px] max-w-[150px] text-sm"
            />
            <div className="flex min-w-0 flex-col gap-0.5">
              <Input
                data-testid="audit-filter-since"
                aria-label="Since"
                placeholder="Since (2026-09-01)"
                value={draft.since}
                onChange={(event) => setDraft({ ...draft, since: event.target.value })}
                className="h-8 min-w-[130px] max-w-[190px] text-sm"
              />
              {timeError?.field === "since" ? (
                <span data-testid="audit-since-error" className="text-[10px] text-destructive">
                  {timeError.message}
                </span>
              ) : null}
            </div>
            <div className="flex min-w-0 flex-col gap-0.5">
              <Input
                data-testid="audit-filter-until"
                aria-label="Until"
                placeholder="Until (2026-09-16)"
                value={draft.until}
                onChange={(event) => setDraft({ ...draft, until: event.target.value })}
                className="h-8 min-w-[130px] max-w-[190px] text-sm"
              />
              {timeError?.field === "until" ? (
                <span data-testid="audit-until-error" className="text-[10px] text-destructive">
                  {timeError.message}
                </span>
              ) : null}
            </div>
            <Button size="sm" data-testid="audit-apply" onClick={applyFilters}>
              Apply
            </Button>
            <Button variant="outline" size="sm" asChild>
              <a data-testid="audit-export" href={exportHref} download>
                <Download aria-hidden />
                Export CSV
              </a>
            </Button>
          </div>

          <p data-testid="audit-export-note" className="max-w-3xl text-[11px] text-muted-foreground">
            The export carries the filters above, up to 5,000 rows. It is{" "}
            <strong className="font-medium">appliance-wide</strong>: the audit log has no tenant
            column, so it holds every tenant&rsquo;s rows — the same rows this table shows. The
            tenant in the filename names who exported it, not what is inside.
          </p>

          {eventsError ? (
            <p
              data-testid="audit-events-error"
              className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
            >
              The audit query failed ({eventsError}). Nothing below is a complete answer — this is
              not an empty log.
            </p>
          ) : null}

          {eventsLoading && events.length === 0 ? (
            <div
              className="flex items-center gap-2 p-6 text-xs text-muted-foreground"
              data-testid="audit-events-loading"
            >
              <Loader2 aria-hidden className="size-4 animate-spin" />
              Reading the audit log…
            </div>
          ) : null}

          {!eventsLoading && !eventsError && events.length === 0 ? (
            <EmptyState
              testId="audit-events-empty"
              icon={ScrollText}
              title="No events match those filters"
              description="The audit log records what the platform decided, not what it was asked. A window with nothing in it means nothing happened in it."
            />
          ) : null}

          {events.length > 0 ? (
            <div className="min-w-0 overflow-x-auto rounded-lg border border-border">
              <table className="w-full min-w-[640px] text-left text-xs">
                <thead className="bg-muted/50 text-[10px] uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-2 py-1.5 font-medium">When</th>
                    <th className="px-2 py-1.5 font-medium">Event</th>
                    <th className="px-2 py-1.5 font-medium">Actor</th>
                    <th className="px-2 py-1.5 font-medium">Action</th>
                    <th className="px-2 py-1.5 font-medium">Target</th>
                    <th className="px-2 py-1.5 font-medium">Status</th>
                    <th className="px-2 py-1.5 font-medium">Channel</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((event) => (
                    <tr
                      key={event.id}
                      data-testid={`audit-event-${event.id}`}
                      className="border-t border-border align-top"
                    >
                      <td className="whitespace-nowrap px-2 py-1.5 text-muted-foreground">
                        {when(event.timestamp)}
                      </td>
                      <td className="px-2 py-1.5 font-mono text-[11px] text-foreground">
                        {event.eventType}
                      </td>
                      <td className="px-2 py-1.5 text-muted-foreground">{event.actor ?? "—"}</td>
                      <td className="px-2 py-1.5 text-muted-foreground">{event.action ?? "—"}</td>
                      <td className="px-2 py-1.5 text-muted-foreground">{event.target ?? "—"}</td>
                      <td className={`px-2 py-1.5 ${statusClass(event.status)}`}>
                        {event.status ?? "—"}
                      </td>
                      <td className="px-2 py-1.5 text-muted-foreground">
                        {event.sourceChannel ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}

          {pageWasFull && eventLimit < EVENT_MAX ? (
            <div className="flex flex-col items-center gap-1 pb-2">
              <Button
                variant="outline"
                size="sm"
                data-testid="audit-events-more"
                disabled={eventsLoading}
                onClick={() => setEventLimit((current) => Math.min(current * 2, EVENT_MAX))}
              >
                {eventsLoading ? "Loading…" : "Load more"}
              </Button>
              <span className="text-[10px] text-muted-foreground">
                This route has no cursor — “more” re-reads a bigger window, up to {EVENT_MAX} rows.
                Export for anything larger.
              </span>
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "flags" && !forbidden ? (
        <div className="flex min-w-0 flex-col gap-3" data-testid="audit-flags">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Input
              data-testid="audit-flag-filter"
              aria-label="Flag"
              placeholder="Flag (ROBOTHOR_…)"
              value={flagDraft}
              onChange={(event) => setFlagDraft(event.target.value)}
              className="h-8 min-w-[160px] max-w-[260px] text-sm"
            />
            <Button
              size="sm"
              data-testid="audit-flag-apply"
              onClick={() => {
                setChanges([]);
                setChangeCursor(null);
                setFlag(flagDraft);
              }}
            >
              Apply
            </Button>
          </div>

          {changesError ? (
            <p
              data-testid="audit-changes-error"
              className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
            >
              {changesError}
            </p>
          ) : null}

          {changesLoading && changes.length === 0 ? (
            <div
              className="flex items-center gap-2 p-6 text-xs text-muted-foreground"
              data-testid="audit-changes-loading"
            >
              <Loader2 aria-hidden className="size-4 animate-spin" />
              Reading the guardrail change log…
            </div>
          ) : null}

          {!changesLoading && !changesError && changes.length === 0 ? (
            <EmptyState
              testId="audit-changes-empty"
              icon={ScrollText}
              title="No guardrail has been changed"
              description="Every write through Settings › Flags lands here with the reason its operator typed. An empty list means the flags are as the instance shipped them."
            />
          ) : null}

          <div className="flex min-w-0 flex-col gap-2">
            {changes.map((change) => (
              <div
                key={change.id}
                data-testid={`audit-change-${change.id}`}
                className="flex min-w-0 flex-col gap-1.5 rounded-lg border border-border bg-card p-3"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="min-w-0 break-all font-mono text-[12px] text-foreground">
                    {change.flag}
                  </span>
                  <span
                    data-testid={`audit-old-${change.id}`}
                    className="shrink-0 rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] text-muted-foreground"
                  >
                    {change.oldValue ?? "unset"}
                  </span>
                  <span aria-hidden className="text-[10px] text-muted-foreground">
                    →
                  </span>
                  <span
                    data-testid={`audit-new-${change.id}`}
                    className="shrink-0 rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-foreground"
                  >
                    {change.newValue ?? "unset"}
                  </span>
                  <span className="ml-auto shrink-0 text-[10px] text-muted-foreground">
                    {when(change.changedAt)}
                  </span>
                </div>
                <p className="text-[11px] text-muted-foreground">
                  {change.reason ?? "No reason recorded."}
                </p>
                <p className="text-[10px] text-muted-foreground/80">
                  Changed by {change.changedBy ?? "an unrecorded caller"}
                </p>
              </div>
            ))}
          </div>

          {changeCursor !== null ? (
            <div className="flex justify-center pb-2">
              <Button
                variant="outline"
                size="sm"
                data-testid="audit-changes-more"
                disabled={changesLoading}
                onClick={() => void loadChanges(changeCursor)}
              >
                {changesLoading ? "Loading…" : "Load more"}
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default AuditView;
