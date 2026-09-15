"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CalendarClock, Loader2, RefreshCw } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";
import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import { isOperatorRole } from "@/components/layout/nav-config";
import { AutomationCard } from "@/components/views/automations/automation-card";
import { WorkflowsSection } from "@/components/views/automations/workflows-section";
import { describeTrigger } from "@/components/views/agents/agent-manifests";
import { normalizeAutomations, type Automation } from "@/lib/automations/run-truth";
import { readBridgeReply } from "@/lib/bridge/read-reply";

/**
 * Automations — every scheduled agent, and what its last run actually did.
 *
 * Mounted at `?v=workflows`, which is the view id the sidebar and every
 * existing link already use; the label has been "Automations" since B6 and only
 * the contents were still a bare workflow-run listing. Keeping the id is what
 * lets a bookmark, a deep link and the nav tests survive the replacement.
 *
 * Two sections, two bridge routes, two independent failure modes. Above: the
 * cron'd agents, from `GET /api/automations` — a composition of the manifest,
 * `agent_schedules` and the newest `agent_runs` row, which is the only place
 * those three have ever been joined. Below: workflows, unchanged.
 *
 * It polls every 60 s **while visible**. `AppShell` mounts every view and hides
 * them with `display: none`, so an ungated effect here would put an
 * operator-gated manifest-directory scan on every Helm page load whatever the
 * operator was actually looking at.
 *
 * Role gating is UX only: the bridge calls `require_operator` on the listing
 * and on every act below it.
 */

const BRIDGE = "/api/bridge";
const POLL_MS = 60_000;

export interface AutomationsViewProps {
  visible?: boolean;
  /** Session role. UX gate only. */
  role?: string | null;
  /** The session has not resolved yet, so the role is unknown — not "denied". */
  roleLoading?: boolean;
}

export function AutomationsView({
  visible = true,
  role,
  roleLoading = false,
}: AutomationsViewProps) {
  const [automations, setAutomations] = useState<Automation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A 403 is the bridge saying this listing belongs to the operator, not the
  // appliance saying it is broken. Told apart so a member is not shown red.
  const [forbidden, setForbidden] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyRow, setBusyRow] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [rowNotes, setRowNotes] = useState<Record<string, string>>({});

  const canWrite = isOperatorRole(role);
  const readOnly = !roleLoading && !canWrite;

  // The load function is re-created on every render-relevant change; the poll
  // must not be, or the interval would be torn down and rebuilt each tick.
  const loadRef = useRef<() => Promise<void>>(async () => {});

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE}/api/automations`);
      if (!res.ok) {
        if (res.status === 403) {
          setForbidden(true);
          setError(null);
          return;
        }
        setError(await readBridgeReply(res));
        return;
      }
      setForbidden(false);
      setAutomations(normalizeAutomations(await res.json()));
      setError(null);
    } catch {
      setError("The dashboard could not reach the bridge. Check that the service is running.");
    } finally {
      setLoading(false);
    }
  }, []);

  loadRef.current = load;

  useEffect(() => {
    if (!visible) return;
    void loadRef.current();
    const timer = setInterval(() => void loadRef.current(), POLL_MS);
    return () => clearInterval(timer);
  }, [visible]);

  function setRow(
    setter: React.Dispatch<React.SetStateAction<Record<string, string>>>,
    id: string,
    message: string | null
  ) {
    setter((prev) => {
      const next = { ...prev };
      if (message) next[id] = message;
      else delete next[id];
      return next;
    });
  }

  const act = useCallback(
    async (id: string, url: string, init: RequestInit, onOk: (body: unknown) => void) => {
      setBusyRow(id);
      setRow(setRowErrors, id, null);
      setRow(setRowNotes, id, null);
      try {
        const res = await fetch(url, {
          headers: { "Content-Type": "application/json" },
          ...init,
        });
        if (!res.ok) {
          setRow(setRowErrors, id, await readBridgeReply(res));
          return;
        }
        onOk(await res.json());
      } catch {
        setRow(setRowErrors, id, "The dashboard could not reach the bridge to do that.");
      } finally {
        setBusyRow(null);
      }
    },
    []
  );

  const manifestUrl = (id: string, suffix = "") =>
    `${BRIDGE}/api/agent-manifests/${encodeURIComponent(id)}${suffix}`;

  const onToggle = useCallback(
    async (automation: Automation) => {
      const suffix = automation.enabled ? "/disable" : "/enable";
      await act(automation.id, manifestUrl(automation.id, suffix), { method: "POST" }, () => {
        setRow(
          setRowNotes,
          automation.id,
          automation.enabled ? "Taken off its schedule." : "Back on its schedule."
        );
        void loadRef.current();
      });
    },
    [act]
  );

  const onRunNow = useCallback(
    async (automation: Automation) => {
      await act(automation.id, manifestUrl(automation.id, "/run"), { method: "POST" }, (body) => {
        setRow(
          setRowNotes,
          automation.id,
          describeTrigger((body as { triggered?: unknown })?.triggered)
        );
        void loadRef.current();
      });
    },
    [act]
  );

  const onResetBreaker = useCallback(
    async (automation: Automation) => {
      const url = `${BRIDGE}/api/automations/${encodeURIComponent(automation.id)}/reset-breaker`;
      await act(automation.id, url, { method: "POST" }, () => {
        setRow(
          setRowNotes,
          automation.id,
          "The error count is back to zero — it runs again on its next scheduled fire."
        );
        void loadRef.current();
      });
    },
    [act]
  );

  const onSaveSchedule = useCallback(
    async (automation: Automation, edit: { cron: string; timezone: string; change: string }) => {
      await act(
        automation.id,
        manifestUrl(automation.id),
        {
          method: "PATCH",
          body: JSON.stringify({
            cron: edit.cron,
            timezone: edit.timezone,
            // The manifest's changelog wants a sentence; an empty note would
            // land an anonymous version bump nobody can read back later.
            change: edit.change || "Schedule changed from the Helm.",
          }),
        },
        () => {
          setRow(setRowNotes, automation.id, "Schedule saved. The engine re-derived its jobs.");
          void loadRef.current();
        }
      );
    },
    [act]
  );

  const rows = automations ?? [];

  return (
    <div
      data-testid="automations-view"
      className="h-full w-full flex-col gap-3 overflow-y-auto p-4"
      style={{ display: visible ? "flex" : "none" }}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <PageHeader
          title="Automations"
          description={
            rows.length > 0
              ? `${rows.length} scheduled ${rows.length === 1 ? "agent" : "agents"}`
              : undefined
          }
        />
        <Button
          variant="outline"
          size="sm"
          data-testid="automations-refresh"
          onClick={() => void loadRef.current()}
        >
          <RefreshCw aria-hidden />
          Refresh
        </Button>
      </div>

      {readOnly && !forbidden ? (
        <p className="text-xs text-muted-foreground" data-testid="automations-readonly">
          You can see what each automation has done, but running them, changing their schedules and
          resetting a breaker are the operator&apos;s to do. Ask an owner or admin on this instance.
        </p>
      ) : null}

      {forbidden ? (
        <p className="text-xs text-muted-foreground" data-testid="automations-forbidden">
          Automations are operator-only on this appliance, so there is nothing to show here. Ask an
          owner or admin on this instance.
        </p>
      ) : null}

      {loading && !automations && !forbidden && !error ? (
        <div
          className="flex items-center gap-2 rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground"
          data-testid="automations-loading"
        >
          <Loader2 aria-hidden className="size-4 animate-spin" />
          Reading the schedule and its last runs…
        </div>
      ) : null}

      {error ? (
        <div
          className="flex flex-col gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4"
          data-testid="automations-error"
        >
          <p className="text-sm font-medium text-destructive">The automations listing failed</p>
          <p className="text-xs text-muted-foreground">{error}</p>
          <div>
            <Button variant="outline" size="sm" onClick={() => void loadRef.current()}>
              Try again
            </Button>
          </div>
        </div>
      ) : null}

      {!error && !forbidden && !loading && rows.length === 0 ? (
        <EmptyState
          testId="automations-empty"
          icon={CalendarClock}
          title="Nothing is on a schedule yet"
          description="An agent becomes an automation when it has a cron. Give one a schedule from the Agents view and it appears here with its runs."
        />
      ) : null}

      <div className="flex flex-col gap-2">
        {rows.map((automation) => (
          <AutomationCard
            key={automation.id}
            automation={automation}
            canWrite={canWrite || roleLoading}
            busy={busyRow === automation.id}
            error={rowErrors[automation.id]}
            note={rowNotes[automation.id]}
            onToggle={onToggle}
            onRunNow={onRunNow}
            onResetBreaker={onResetBreaker}
            onSaveSchedule={onSaveSchedule}
          />
        ))}
      </div>

      <WorkflowsSection visible={visible} />
    </div>
  );
}

export default AutomationsView;
