"use client";

import { useCallback, useMemo, useState } from "react";
import { FileText, Loader2, RefreshCw } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";
import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { isOperatorRole } from "@/components/layout/nav-config";
import { useBridgePoll } from "@/lib/bridge/use-bridge-poll";
import { isLogSince } from "@/lib/observe/since";

/**
 * Observe › Logs — journald, for an operator who is not on the box.
 *
 * **`available: false` is a 200, and must never be red.** A container has no
 * journald, and there is nothing broken about that: both routes answer with a
 * sentence saying why. Every containerised deployment of this app sees that
 * state and only that state, which makes it the most-seen screen in this file
 * and the easiest one to get wrong — an appliance fault reported where there is
 * none sends an operator looking for a problem that does not exist.
 *
 * **The unit picker is the route's allowlist**, read from `GET /api/logs/units`
 * and never a list typed here. That list is derived on the bridge from the unit
 * files `scripts/install-units.sh` installs, so a unit added to `infra/systemd`
 * appears without a code change — and `sshd` never does.
 *
 * **Auto-refresh is off by default.** Ten seconds of journald is a real read on
 * the box (one `journalctl` process per request), and the shell keeps every
 * view mounted, so an ungated poll would be reading the journal on every Helm
 * page load. When it is on, `useBridgePoll` pauses it the moment the view goes
 * off screen.
 *
 * **A `since` the bridge would refuse never leaves the page.** `lib/observe/
 * since.ts` mirrors the route's own pattern, so a typo is a line under the
 * field. A 422 that does come back is placed under the field it names rather
 * than in a banner — `_params` writes "<field> must be …", and the field is the
 * only thing the operator can act on.
 *
 * `truncated` is measured on the bridge against what journald RETURNED, not
 * what survived `grep`. So the notice says the window was full, never that
 * there are no more matches.
 */

const BRIDGE = "/api/bridge";

/** One beat for a pane an operator is actively watching. */
const LOGS_POLL_MS = 10_000;

const LINE_CHOICES = [50, 200, 1000];

const SINCE_PRESETS: Array<{ id: string; value: string; label: string }> = [
  { id: "15m", value: "15m", label: "15m" },
  { id: "1h", value: "1h", label: "1h" },
  { id: "24h", value: "24h", label: "24h" },
];

interface Unit {
  name: string;
  description: string;
}

interface Line {
  ts: string | null;
  priority: number | null;
  message: string;
}

type Level = "error" | "warning" | "info" | "unknown";

/** syslog, as journald writes it: 0–3 is a failure, 4 is a warning. */
export function levelOf(priority: number | null): Level {
  if (priority === null) return "unknown";
  if (priority <= 3) return "error";
  if (priority === 4) return "warning";
  return "info";
}

const LEVEL_CLASS: Record<Level, string> = {
  error: "border-destructive/30 bg-destructive/10 text-destructive",
  warning: "border-warning/30 bg-warning/10 text-warning",
  info: "border-border bg-muted text-muted-foreground",
  unknown: "border-border bg-muted text-muted-foreground/70",
};

/**
 * Whether a refusal belongs under the one field that has a slot for it.
 *
 * `routers/_params.py` writes every refusal as "<field> must be …", so the
 * first word of the bridge's own sentence names the input that produced it —
 * and `since` is the ONLY input on this page with a place to put a message
 * under it. That asymmetry used to be a hole: the page recognised `lines` and
 * `unit` too, routed them at slots that do not exist, rendered them nowhere,
 * AND suppressed the empty state on the way past, so a `lines` 422 came back
 * as a blank screen.
 *
 * So there is one name here, and everything else — including a reworded
 * `unit must be one of: …` — falls through to the banner. Adding a name to
 * this list without adding the slot beside the input is the bug again.
 */
const SINCE_REFUSAL = /^since\b/i;

export function isSinceRefusal(message: string): boolean {
  return SINCE_REFUSAL.test(message.trim());
}

/**
 * A fixed-width stand-in, not blank space: eight spaces collapse in HTML and
 * take the column with them, so the message jumps left and the pane stops
 * lining up at exactly the lines journald recorded least about.
 */
const NO_TIME = "--:--:--";

function timeOf(value: string | null): string {
  if (!value) return NO_TIME;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleTimeString("en-US", { hour12: false });
}

export interface LogsViewProps {
  /** The shell keeps every view mounted; this is the load gate. */
  visible?: boolean;
  /** Session role. UX only — the bridge gates both log routes itself. */
  role?: string | null;
  /** The session has not resolved yet, so the role is unknown — not "denied". */
  roleLoading?: boolean;
}

export function LogsView({ visible = true, role, roleLoading = false }: LogsViewProps) {
  const [units, setUnits] = useState<Unit[]>([]);
  const [unitsAvailable, setUnitsAvailable] = useState(true);
  const [unitsReason, setUnitsReason] = useState<string | null>(null);

  const [unit, setUnit] = useState("");
  const [lines, setLines] = useState(200);
  const [since, setSince] = useState("");
  const [grep, setGrep] = useState("");

  const [sinceDraft, setSinceDraft] = useState("");
  const [grepDraft, setGrepDraft] = useState("");
  const [sinceError, setSinceError] = useState<string | null>(null);

  const [pane, setPane] = useState<Line[]>([]);
  const [paneAvailable, setPaneAvailable] = useState(true);
  const [paneReason, setPaneReason] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);

  const [autoRefresh, setAutoRefresh] = useState(false);

  const operator = isOperatorRole(role);
  const mayRead = operator || roleLoading;

  const onUnits = useCallback((body: unknown) => {
    const root = (body ?? {}) as Record<string, unknown>;
    const list = Array.isArray(root.units)
      ? root.units
          .map((entry) => {
            if (!entry || typeof entry !== "object") return null;
            const row = entry as Record<string, unknown>;
            if (typeof row.name !== "string" || !row.name) return null;
            return {
              name: row.name,
              description: typeof row.description === "string" ? row.description : "",
            };
          })
          .filter((u): u is Unit => u !== null)
      : [];
    setUnits(list);
    setUnitsAvailable(root.available !== false);
    setUnitsReason(typeof root.reason === "string" ? root.reason : null);
    setUnit((current) => (current && list.some((u) => u.name === current) ? current : (list[0]?.name ?? "")));
  }, []);

  const unitsPoll = useBridgePoll({
    visible: visible && operator,
    url: `${BRIDGE}/api/logs/units`,
    onData: onUnits,
    // The allowlist is derived from unit files and cached 30 s on the bridge.
    // It has no reason to be re-read on a beat.
    paused: true,
  });

  const linesUrl = useMemo(() => {
    const params = new URLSearchParams();
    params.set("unit", unit);
    params.set("lines", String(lines));
    if (since) params.set("since", since);
    if (grep) params.set("grep", grep);
    return `${BRIDGE}/api/logs?${params.toString()}`;
  }, [unit, lines, since, grep]);

  const onLines = useCallback((body: unknown) => {
    const root = (body ?? {}) as Record<string, unknown>;
    setPaneAvailable(root.available !== false);
    setPaneReason(typeof root.reason === "string" ? root.reason : null);
    setTruncated(root.truncated === true);
    setPane(
      Array.isArray(root.lines)
        ? root.lines
            .map((entry) => {
              if (!entry || typeof entry !== "object") return null;
              const row = entry as Record<string, unknown>;
              return {
                ts: typeof row.ts === "string" ? row.ts : null,
                // Absent stays absent: an invented priority is a claim about a
                // line's severity that journald did not make.
                priority: typeof row.priority === "number" ? row.priority : null,
                message: typeof row.message === "string" ? row.message : "",
              };
            })
            .filter((l): l is Line => l !== null)
        : []
    );
  }, []);

  /**
   * Whether a read is even possible. It is also the gate on the read's
   * spinner: `useBridgePoll.loading` starts `true` and only settles when a
   * request actually happens, so a page that renders "Reading the journal…"
   * whenever `loading` is true spins for ever on every state where no read is
   * issued — a refused catalog, an empty one, a deployment with no journald.
   */
  const canRead = visible && operator && unitsAvailable && units.length > 0 && unit !== "";

  const linesPoll = useBridgePoll({
    visible: canRead,
    url: linesUrl,
    onData: onLines,
    intervalMs: LOGS_POLL_MS,
    paused: !autoRefresh,
  });

  /**
   * Where each answer goes. The rule is that every one of them goes SOMEWHERE.
   *
   * A 422 naming `since` has a slot under that input; every other refusal —
   * `lines`, a reworded `unit`, a 500, anything — goes in the banner. A 403 is
   * neither: it is the bridge saying this is not yours, and it is rendered the
   * way the rest of the Helm renders one (muted, not red) rather than as a
   * claim about the journal.
   */
  const forbidden = unitsPoll.forbidden || linesPoll.forbidden;
  const sinceRefused =
    linesPoll.status === 422 && linesPoll.error !== null && isSinceRefusal(linesPoll.error);
  const bannerError = forbidden
    ? null
    : (unitsPoll.error ?? (sinceRefused ? null : linesPoll.error));
  const sinceMessage = sinceError ?? (sinceRefused ? linesPoll.error : null);

  const onSinceDraft = useCallback((value: string) => {
    setSinceDraft(value);
    // Clearing the local complaint as soon as the value becomes valid keeps one
    // message under the field at a time.
    if (value === "" || isLogSince(value)) setSinceError(null);
  }, []);

  const refresh = useCallback(() => {
    const nextSince = sinceDraft.trim();
    if (nextSince && !isLogSince(nextSince)) {
      setSinceError(
        "since must be a relative age (30s, 15m, 1h, 7d) or an ISO-8601 date — journald's English is not accepted here."
      );
      return;
    }
    setSinceError(null);
    const nextGrep = grepDraft;
    const unchanged = nextSince === since && nextGrep === grep;
    setSince(nextSince);
    setGrep(nextGrep);
    // A changed value moves the URL and the poll re-reads on its own; an
    // unchanged one has to be asked for, which is what Refresh is for.
    if (unchanged) linesPoll.reload();
  }, [sinceDraft, grepDraft, since, grep, linesPoll]);

  if (!visible) return null;

  if (!mayRead) {
    return (
      <div className="flex h-full flex-col gap-3 overflow-y-auto p-4" data-testid="logs-view">
        <PageHeader title="Logs" />
        <EmptyState
          testId="logs-not-yours"
          icon={FileText}
          title="The journal is an operator screen"
          description="A unit's journal carries every value every process printed — a far wider surface than a record of what was decided — so it is owner and admin only. An auditor reads the audit trail instead."
        />
      </div>
    );
  }

  const unavailableReason = !unitsAvailable
    ? (unitsReason ?? "journald is not available on this deployment.")
    : !paneAvailable
      ? (paneReason ?? "journald could not be read on this deployment.")
      : null;

  /**
   * journalctl is here and the catalog is empty. A real answer from
   * `routers/logs.py`, not a transient: the allowlist is derived from the
   * `robothor-*.service` files the installer renders, and a dev checkout with
   * no units installed and no `infra/systemd` fallback produces exactly this.
   * It used to spin for ever, because there is no unit to read and therefore
   * no read to settle the loading flag.
   */
  const noUnits =
    !forbidden &&
    unitsPoll.error === null &&
    !unitsPoll.loading &&
    unitsAvailable &&
    units.length === 0;

  // Only the phase that is actually in flight may show a spinner.
  const catalogLoading = unitsPoll.loading && !forbidden && unitsPoll.error === null;
  const readLoading = canRead && linesPoll.loading && pane.length === 0;

  return (
    <div className="flex h-full min-w-0 flex-col gap-3 overflow-hidden p-4" data-testid="logs-view">
      <PageHeader title="Logs" description="The journal of the units this instance runs.">
        <button
          type="button"
          aria-pressed={autoRefresh}
          data-testid="logs-auto"
          onClick={() => setAutoRefresh((current) => !current)}
          className={`rounded-md border px-2 py-1 text-xs transition-colors ${
            autoRefresh
              ? "border-primary/25 bg-primary/10 text-foreground"
              : "border-border text-muted-foreground hover:text-foreground"
          }`}
        >
          Auto-refresh {autoRefresh ? "on" : "off"}
        </button>
        <Button variant="outline" size="sm" data-testid="logs-refresh" onClick={refresh}>
          <RefreshCw aria-hidden className={linesPoll.loading ? "animate-spin" : undefined} />
          Refresh
        </Button>
      </PageHeader>

      {forbidden ? (
        <p className="text-xs text-muted-foreground" data-testid="logs-forbidden">
          The journal is operator-only on this appliance, so there is nothing to show here. A role
          that looks like an operator in this browser can still be refused by the bridge — it also
          requires the platform tenant and a human session. Ask an owner or admin on this instance.
        </p>
      ) : catalogLoading ? (
        <div
          className="flex items-center gap-2 p-6 text-xs text-muted-foreground"
          data-testid="logs-loading"
        >
          <Loader2 aria-hidden className="size-4 animate-spin" />
          Reading the units this instance runs…
        </div>
      ) : bannerError && units.length === 0 ? (
        <p
          data-testid="logs-error"
          className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
        >
          {bannerError}
        </p>
      ) : unavailableReason ? (
        <EmptyState
          testId="logs-unavailable"
          icon={FileText}
          title="Logs are not available on this deployment"
          description={`${unavailableReason} Nothing is broken — a container has no journald. On a systemd install, the bridge's service user also has to be able to read the journals it is asked for.`}
        />
      ) : noUnits ? (
        <EmptyState
          testId="logs-no-units"
          icon={FileText}
          title="journald is here, but no unit is installed"
          description="The followable units are derived from the robothor-*.service files the installer renders, and this deployment has none. Install the units (scripts/install-units.sh) and refresh; nothing else about this instance is wrong."
        />
      ) : (
        <>
          <div className="flex min-w-0 flex-wrap items-start gap-2">
            <NativeSelect
              data-testid="logs-unit"
              aria-label="Unit"
              value={unit}
              onChange={(event) => setUnit(event.target.value)}
              className="max-w-[240px]"
            >
              {units.map((entry) => (
                <option key={entry.name} value={entry.name}>
                  {entry.name}
                </option>
              ))}
            </NativeSelect>

            <NativeSelect
              data-testid="logs-lines"
              aria-label="Lines"
              value={String(lines)}
              onChange={(event) => setLines(Number(event.target.value))}
            >
              {LINE_CHOICES.map((count) => (
                <option key={count} value={count}>
                  {count} lines
                </option>
              ))}
            </NativeSelect>

            <div
              role="group"
              aria-label="Since"
              className="flex shrink-0 items-center gap-1 rounded-md border border-border p-0.5"
            >
              {SINCE_PRESETS.map((preset) => (
                <button
                  key={preset.id}
                  type="button"
                  aria-pressed={since === preset.value && sinceDraft === ""}
                  data-testid={`logs-since-${preset.id}`}
                  onClick={() => {
                    setSinceDraft("");
                    setSinceError(null);
                    setSince(preset.value);
                  }}
                  className={`rounded px-2 py-1 text-xs transition-colors ${
                    since === preset.value && sinceDraft === ""
                      ? "bg-primary/15 text-foreground"
                      : "text-muted-foreground hover:bg-accent/60"
                  }`}
                >
                  {preset.label}
                </button>
              ))}
            </div>

            <div className="flex min-w-0 flex-col gap-0.5">
              <Input
                data-testid="logs-since-custom"
                aria-label="Since (custom)"
                placeholder="or 45m / 2026-09-15"
                value={sinceDraft}
                onChange={(event) => onSinceDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") refresh();
                }}
                className="h-8 min-w-[120px] max-w-[180px] text-sm"
              />
              {sinceMessage ? (
                <span
                  data-testid="logs-since-error"
                  className="max-w-[260px] text-[10px] text-destructive"
                >
                  {sinceMessage}
                </span>
              ) : null}
            </div>

            <Input
              data-testid="logs-grep"
              aria-label="Filter"
              placeholder="Filter (applied after redaction)"
              value={grepDraft}
              onChange={(event) => setGrepDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") refresh();
              }}
              className="h-8 min-w-[150px] max-w-[240px] text-sm"
            />
          </div>

          {bannerError ? (
            <p
              data-testid="logs-error"
              className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
            >
              {bannerError}
            </p>
          ) : null}

          {truncated ? (
            <p
              data-testid="logs-truncated"
              className="max-w-3xl rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-[11px] text-warning"
            >
              Window full — this is the last {lines} lines journald returned, not the whole period.
              Raise the line count or narrow the time range to be sure of what is in it.
            </p>
          ) : null}

          {readLoading ? (
            <div
              className="flex items-center gap-2 p-6 text-xs text-muted-foreground"
              data-testid="logs-loading"
            >
              <Loader2 aria-hidden className="size-4 animate-spin" />
              Reading the journal…
            </div>
          ) : null}

          {/*
            An empty pane is only "nothing matched" when the read SUCCEEDED.
            Any refusal — banner or field — means the page has no idea what is
            in that window, and saying it is empty would be a claim the server
            never made.
          */}
          {!readLoading && pane.length === 0 && !bannerError && !sinceMessage ? (
            <EmptyState
              testId="logs-empty"
              icon={FileText}
              title="Nothing in that window"
              description="No line of this unit's journal matched. The filter is applied to the redacted message, so a search for a secret finds nothing by design."
            />
          ) : null}

          {pane.length > 0 ? (
            <div
              data-testid="logs-pane"
              className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-card p-2 font-mono text-[11px] leading-relaxed"
            >
              {pane.map((line, index) => {
                const level = levelOf(line.priority);
                return (
                  <div
                    key={`${line.ts ?? "no-ts"}-${index}`}
                    data-testid={`logs-line-${index}`}
                    className="flex min-w-0 items-start gap-2 px-1 py-0.5 hover:bg-accent/40"
                  >
                    <span className="shrink-0 tabular-nums text-muted-foreground/70">
                      {timeOf(line.ts)}
                    </span>
                    <span
                      data-testid={`logs-priority-${index}`}
                      data-level={level}
                      className={`shrink-0 rounded border px-1 text-[9.5px] uppercase ${LEVEL_CLASS[level]}`}
                    >
                      {line.priority ?? "?"}
                    </span>
                    <span className="min-w-0 whitespace-pre-wrap break-words text-foreground">
                      {line.message}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : null}

          <p className="shrink-0 text-[10px] text-muted-foreground">
            Every message is passed through the platform&rsquo;s secret redactor before it leaves the
            bridge, and the filter is applied to the redacted text — so this pane cannot be used to
            confirm a credential it will not show.
          </p>
        </>
      )}
    </div>
  );
}

export default LogsView;
