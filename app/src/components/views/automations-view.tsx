"use client";

import { useCallback, useState } from "react";
import { CalendarClock, Loader2, RefreshCw } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";
import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import { isOperatorRole } from "@/components/layout/nav-config";
import { AutomationCard } from "@/components/views/automations/automation-card";
import { WorkflowsSection } from "@/components/views/automations/workflows-section";
import { describeTrigger } from "@/components/views/agents/agent-manifests";
import { reconcileNote, warningLine, warningsOf } from "@/lib/agents/reconcile";
import { normalizeAutomations, type Automation } from "@/lib/automations/run-truth";
import { useRowActions } from "@/lib/bridge/row-actions";
import { useBridgePoll } from "@/lib/bridge/use-bridge-poll";

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
  // The busy row, the per-row error and the per-row note, shared with the
  // Agents view's manifest list rather than forked from it.
  const { busyRow, rowErrors, rowNotes, setRowNote, act } = useRowActions();
  // The engine's half of a manifest write, kept per row beside the note.
  const [rowReconcile, setRowReconcile] = useState<Record<string, string>>({});
  const [rowWarnings, setRowWarnings] = useState<Record<string, string>>({});

  const canWrite = isOperatorRole(role);
  const readOnly = !roleLoading && !canWrite;

  // Poll-while-visible, the 403 that is not an error, and the one sentence for
  // an unreachable bridge all live in `useBridgePoll` now — this block used to
  // be one of three byte-near copies of them.
  const absorbListing = useCallback(
    (body: unknown) => setAutomations(normalizeAutomations(body)),
    []
  );
  const { loading, error, forbidden, reload } = useBridgePoll({
    visible,
    url: `${BRIDGE}/api/automations`,
    onData: absorbListing,
  });

  /**
   * Read the engine's half of a write and say only what it supports.
   *
   * The routes behind these buttons answer `{"reconcile": {"applied": false,
   * "error": …}}` with a 200 when the engine is unreachable, and
   * `agent_manifests`'s own docstring names the failure: the operator must not
   * be told the agent is live when it is not. Same reader as B8's panel.
   */
  const absorb = useCallback(
    (id: string, body: unknown) => {
      setRowReconcile((prev) => ({ ...prev, [id]: reconcileNote(body) ?? "" }));
      setRowWarnings((prev) => ({ ...prev, [id]: warningLine(warningsOf(body)) ?? "" }));
    },
    []
  );

  // The card is keyed by JOB id (`main:heartbeat`); the manifest routes are
  // keyed by the AGENT id. Conflating the two is what made the reset address a
  // row that does not exist.
  const manifestUrl = (automation: Automation, suffix = "") =>
    `${BRIDGE}/api/agent-manifests/${encodeURIComponent(automation.agent_id)}${suffix}`;

  const onToggle = useCallback(
    async (automation: Automation) => {
      // `schedule.enabled` rides every job spec the manifest derives, so one
      // disable stops the agent, its heartbeat and its worker together.
      const suffix = automation.enabled ? "/disable" : "/enable";
      await act(automation.id, manifestUrl(automation, suffix), { method: "POST" }, (body) => {
        setRowNote(
          automation.id,
          automation.enabled ? "Taken off its schedule." : "Back on its schedule."
        );
        absorb(automation.id, body);
        reload();
      });
    },
    [act, absorb, reload, setRowNote]
  );

  const onRunNow = useCallback(
    async (automation: Automation) => {
      await act(automation.id, manifestUrl(automation, "/run"), { method: "POST" }, (body) => {
        setRowNote(automation.id, describeTrigger((body as { triggered?: unknown })?.triggered));
        absorb(automation.id, body);
        reload();
      });
    },
    [act, absorb, reload, setRowNote]
  );

  const onResetBreaker = useCallback(
    async (automation: Automation) => {
      // By JOB id: the scheduler trips its breaker per job, so resetting the
      // bare agent id would leave the stopped heartbeat stopped.
      const url = `${BRIDGE}/api/automations/${encodeURIComponent(automation.id)}/reset-breaker`;
      await act(automation.id, url, { method: "POST" }, (body) => {
        setRowNote(
          automation.id,
          "The error count is back to zero — it runs again on its next scheduled fire."
        );
        absorb(automation.id, body);
        reload();
      });
    },
    [act, absorb, reload, setRowNote]
  );

  const onSaveSchedule = useCallback(
    async (automation: Automation, edit: { cron: string; timezone: string; change: string }) => {
      await act(
        automation.id,
        manifestUrl(automation),
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
        (body) => {
          // "Saved" is what the file did. Whether the engine re-derived its
          // jobs is a separate claim the body has to support.
          setRowNote(
            automation.id,
            reconcileNote(body)
              ? "Schedule saved to the manifest."
              : "Schedule saved. The engine re-derived its jobs."
          );
          absorb(automation.id, body);
          reload();
        }
      );
    },
    [act, absorb, reload, setRowNote]
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
          onClick={reload}
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
            <Button variant="outline" size="sm" onClick={reload}>
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
            reconcile={rowReconcile[automation.id] || null}
            warnings={rowWarnings[automation.id] || null}
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
