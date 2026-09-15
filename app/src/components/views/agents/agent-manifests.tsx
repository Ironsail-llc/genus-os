"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, Pause, Play, Plus, RefreshCw, Rocket } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { isOperatorRole } from "@/components/layout/nav-config";
import type { AgentInfo } from "@/hooks/use-agents";
import {
  describeCron,
  type BrokenManifest,
  type ManifestSummary,
  type ModelEntry,
} from "@/lib/agents/manifests";
import { reconcileNote } from "@/lib/agents/reconcile";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { useRowActions } from "@/lib/bridge/row-actions";

import { AgentPanel } from "./agent-panel";

/**
 * The fleet as manifests: what each agent is for, when it fires, and the four
 * acts an operator performs on one — build it, edit it, run it, retire it.
 *
 * Reads `GET /api/agent-manifests`, which answers with a `broken` bucket
 * alongside the agents. That bucket is rendered, not dropped: a manifest the
 * engine's loader cannot parse is invisible everywhere else in the Helm, and
 * "my agent disappeared" is the exact confusion this section exists to end.
 *
 * Retire, never delete. The bridge moves the file to `retired/` and refuses
 * unless `confirm` is the agent's own id, typed. The same id is typed here —
 * into a real input, never a browser `prompt()` — so the client lock is the
 * same lock, not a weaker one that a boolean would satisfy.
 *
 * Role gating is UX only. The bridge calls `require_operator` on every route
 * above, including the GET.
 */

const BRIDGE = "/api/bridge";

const CELL =
  "flex items-start justify-between gap-3 px-3 py-1.5 text-sm md:table-cell md:py-2.5 md:align-top";

function CellLabel({ children }: { children: string }) {
  return (
    <span className="shrink-0 text-[11px] uppercase tracking-[0.08em] text-muted-foreground md:hidden">
      {children}
    </span>
  );
}

/** What the engine said when it was told to run an agent, as one sentence. */
export function describeTrigger(triggered: unknown): string {
  if (triggered && typeof triggered === "object") {
    const runId = (triggered as { run_id?: unknown }).run_id;
    if (typeof runId === "string" && runId) return `The engine started run ${runId}.`;
    const status = (triggered as { status?: unknown }).status;
    if (typeof status === "string" && status) return `The engine answered: ${status}.`;
  }
  if (typeof triggered === "string" && triggered) return `The engine answered: ${triggered}.`;
  return "The engine accepted the trigger.";
}

export interface AgentManifestsProps {
  /** Session role. UX gate only — the bridge authorizes every route itself. */
  role?: string | null;
  /** The session has not resolved yet, so the role is unknown — not "denied". */
  roleLoading?: boolean;
  /**
   * The engine's health rows, joined onto these manifests BY ID.
   *
   * Two different sources of truth about the same agents: `agent_status` knows
   * how each one has been RUNNING, and this listing knows what each one IS.
   * The join key is `agentId` and never `name` — a display name is a string an
   * operator renames whenever they like, and a name-matched join would put one
   * agent's failures on another agent's row the first time somebody did.
   */
  health?: AgentInfo[];
  /**
   * Whether the Agents view is the one on screen.
   *
   * `AppShell` mounts every view and hides them with `display: none`, so an
   * effect with no gate here fires two operator-gated bridge calls — one of
   * which scans the manifest directory — on every Helm page load, whatever the
   * operator was actually looking at. `fleet-view` and `marketplace-view` both
   * gate on this; this one now does too.
   */
  visible?: boolean;
}

/** Token classes per health tier, matching the pills elsewhere in the Helm. */
function healthClass(tier: string): string {
  if (tier === "healthy") return "border-success/30 bg-success/10 text-success";
  if (tier === "degraded") return "border-warning/30 bg-warning/10 text-warning";
  if (tier === "failed") return "border-destructive/30 bg-destructive/10 text-destructive";
  return "border-border bg-muted text-muted-foreground";
}

export function AgentManifests({
  role,
  roleLoading = false,
  health = [],
  visible = true,
}: AgentManifestsProps) {
  const [agents, setAgents] = useState<ManifestSummary[] | null>(null);
  const [broken, setBroken] = useState<BrokenManifest[]>([]);
  const [listError, setListError] = useState<string | null>(null);
  // A 403 is not a failure of the appliance: it is the bridge saying this
  // listing belongs to the operator. Told apart from every other refusal so a
  // member is not shown a red box claiming their fleet is broken.
  const [refusedAsNonOperator, setRefusedAsNonOperator] = useState(false);
  const [loading, setLoading] = useState(true);

  const [models, setModels] = useState<ModelEntry[]>([]);

  // The busy row, the per-row error and the per-row note. One implementation,
  // shared with the Automations view — these were two byte-near copies, right
  // down to "Taken off its schedule.", and the reconcile fix below landed in
  // only one of them the first time round.
  const { busyRow, rowErrors, rowNotes, setRowNote, act: performRowAction } = useRowActions();
  const [rowReconcile, setRowReconcile] = useState<Record<string, string>>({});
  const [retiring, setRetiring] = useState<{ id: string; typed: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [panel, setPanel] = useState<{ agentId: string | null } | null>(null);

  // An unresolved session is not a refusal: an owner must not be shown a
  // read-only screen for the half second before their own role arrives.
  const canWrite = isOperatorRole(role);
  const readOnly = !roleLoading && !canWrite;

  const loadAgents = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${BRIDGE}/api/agent-manifests`);
      if (!res.ok) {
        if (res.status === 403) {
          setRefusedAsNonOperator(true);
          setListError(null);
          return;
        }
        setListError(await readBridgeReply(res));
        return;
      }
      setRefusedAsNonOperator(false);
      const body = (await res.json()) as { agents?: ManifestSummary[]; broken?: BrokenManifest[] };
      setAgents(body.agents ?? []);
      setBroken(Array.isArray(body.broken) ? body.broken : []);
      setListError(null);
    } catch {
      setListError("The dashboard could not reach the bridge. Check that the service is running.");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadModels = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE}/api/models`);
      if (!res.ok) return;
      const body = (await res.json()) as { models?: ModelEntry[] };
      setModels(body.models ?? []);
    } catch {
      // The catalog is a convenience for the model select; a builder that
      // cannot read it still posts a model the operator typed in the YAML.
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    void loadAgents();
    void loadModels();
  }, [visible, loadAgents, loadModels]);

  /**
   * The engine's half of a write, which a 2xx alone does not settle.
   *
   * `enable`/`disable` reconcile like a PATCH does, and answer
   * `{"reconcile": {"applied": false, …}}` with a 200 when the engine is
   * unreachable. Same reader as the agent panel and the Automations view.
   */
  function absorbReconcile(id: string, body: unknown) {
    setRowReconcile((prev) => ({ ...prev, [id]: reconcileNote(body) ?? "" }));
  }

  async function act(id: string, path: string, init: RequestInit, onOk: (body: unknown) => void) {
    await performRowAction(
      id,
      `${BRIDGE}/api/agent-manifests/${encodeURIComponent(id)}${path}`,
      init,
      onOk
    );
  }

  async function toggle(agent: ManifestSummary) {
    await act(agent.id, agent.enabled ? "/disable" : "/enable", { method: "POST" }, (body) => {
      setRowNote(agent.id, agent.enabled ? "Taken off its schedule." : "Back on its schedule.");
      absorbReconcile(agent.id, body);
      void loadAgents();
    });
  }

  async function runNow(agent: ManifestSummary) {
    await act(agent.id, "/run", { method: "POST" }, (body) => {
      setRowNote(agent.id, describeTrigger((body as { triggered?: unknown })?.triggered));
    });
  }

  async function retire(id: string) {
    await act(id, "", { method: "DELETE", body: JSON.stringify({ confirm: id }) }, () => {
      setRetiring(null);
      setNotice(
        `${id} is retired. Its manifest moved to the retired/ folder beside the others, and its ` +
          "instruction file was left where it is — recreating the agent with the same id picks it back up."
      );
      void loadAgents();
    });
  }

  const rows = agents ?? [];

  const healthById = new Map(
    health.filter((entry) => entry.agentId).map((entry) => [entry.agentId as string, entry])
  );

  return (
    <section className="flex flex-col gap-3" data-testid="agent-manifests">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium text-foreground">Agents</h3>
          <p className="text-xs text-muted-foreground">
            Every manifest this appliance holds. Changes take effect without a restart — the engine
            re-derives its schedule on each save.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            variant="outline"
            size="sm"
            data-testid="agent-manifests-refresh"
            onClick={() => {
              void loadAgents();
              void loadModels();
            }}
          >
            <RefreshCw aria-hidden />
            Refresh
          </Button>
          {canWrite ? (
            <Button
              size="sm"
              data-testid="agent-new"
              onClick={() => setPanel({ agentId: null })}
            >
              <Plus aria-hidden />
              New agent
            </Button>
          ) : null}
        </div>
      </div>

      {readOnly || refusedAsNonOperator ? (
        <p className="text-xs text-muted-foreground" data-testid="agent-manifests-readonly">
          {refusedAsNonOperator
            ? "The fleet listing is operator-only on this appliance, so there is nothing to show here. Ask an owner or admin on this instance."
            : "You can see the fleet, but building, editing, running and retiring agents are the operator's to do. Ask an owner or admin on this instance."}
        </p>
      ) : null}

      {visible && loading && !agents && !refusedAsNonOperator ? (
        <div
          className="flex items-center gap-2 rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground"
          data-testid="agent-manifests-loading"
        >
          <Loader2 aria-hidden className="size-4 animate-spin" />
          Reading the fleet manifests from the bridge…
        </div>
      ) : null}

      {listError ? (
        <div
          className="flex flex-col gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4"
          data-testid="agent-manifests-error"
        >
          <p className="text-sm font-medium text-destructive">The manifest listing failed</p>
          <p className="text-xs text-muted-foreground">{listError}</p>
          <div>
            <Button variant="outline" size="sm" onClick={() => void loadAgents()}>
              Try again
            </Button>
          </div>
        </div>
      ) : null}

      {notice ? (
        <p
          aria-live="polite"
          className="rounded-md border border-border bg-card p-2.5 text-xs text-muted-foreground"
          data-testid="agent-manifests-notice"
        >
          {notice}
        </p>
      ) : null}

      {panel ? (
        <AgentPanel
          key={panel.agentId ?? "__new__"}
          agentId={panel.agentId}
          models={models}
          onClose={() => setPanel(null)}
          onSaved={() => void loadAgents()}
        />
      ) : null}

      {!listError && !refusedAsNonOperator && !loading && rows.length === 0 ? (
        <div
          className="rounded-lg border border-dashed border-border bg-card/40 p-4"
          data-testid="agent-manifests-empty"
        >
          <p className="text-sm font-medium text-foreground">This appliance has no agents yet</p>
          <p className="mt-1 max-w-xl text-xs text-muted-foreground">
            An agent is a name, a job and a set of instructions. Build one with New agent — the
            schedule, model and delivery are all behind Advanced and can wait.
          </p>
        </div>
      ) : null}

      {rows.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-border bg-card">
          <table className="w-full border-collapse text-sm">
            <thead className="hidden md:table-header-group">
              <tr className="border-b border-border text-left text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
                <th className="px-3 py-2 font-medium">Agent</th>
                <th className="px-3 py-2 font-medium">Schedule</th>
                <th className="px-3 py-2 font-medium">Model</th>
                <th className="px-3 py-2 font-medium">Delivery</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="block md:table-row-group">
              {rows.map((agent) => {
                const human = describeCron(agent.cron);
                const rowError = rowErrors[agent.id];
                const rowNote = rowNotes[agent.id];
                const confirming = retiring?.id === agent.id;
                const tier = healthById.get(agent.id);
                return (
                  <tr
                    key={agent.id}
                    data-testid={`agent-row-${agent.id}`}
                    className="block border-b border-border last:border-b-0 md:table-row"
                  >
                    <td className={CELL}>
                      <CellLabel>Agent</CellLabel>
                      <div className="flex flex-col items-end gap-0.5 text-right md:items-start md:text-left">
                        <span className="font-medium text-foreground">{agent.name || agent.id}</span>
                        <span className="font-mono text-[11px] text-muted-foreground">
                          {agent.id}
                        </span>
                        {agent.description ? (
                          <span className="max-w-sm text-xs text-muted-foreground">
                            {agent.description}
                          </span>
                        ) : null}
                        <div className="flex flex-wrap items-center justify-end gap-1.5 md:justify-start">
                          <Badge
                            variant="outline"
                            data-testid={`agent-enabled-${agent.id}`}
                          className={
                            agent.enabled
                              ? "border-success/30 bg-success/10 text-success"
                              : "border-border bg-muted text-muted-foreground"
                          }
                        >
                            {agent.enabled ? "Enabled" : "Disabled"}
                          </Badge>
                          <Badge
                            variant="outline"
                            data-testid={`agent-health-${agent.id}`}
                            className={healthClass(tier?.status ?? "")}
                          >
                            {tier ? tier.status : "no runs recorded"}
                          </Badge>
                        </div>
                      </div>
                    </td>

                    <td className={CELL}>
                      <CellLabel>Schedule</CellLabel>
                      <div className="flex flex-col items-end gap-0.5 text-right md:items-start md:text-left">
                        {agent.cron ? (
                          <span className="font-mono text-xs text-foreground">{agent.cron}</span>
                        ) : null}
                        <span
                          className="text-[11px] text-muted-foreground"
                          data-testid={`agent-cron-human-${agent.id}`}
                        >
                          {human}
                        </span>
                        {agent.timezone ? (
                          <span className="text-[11px] text-muted-foreground">{agent.timezone}</span>
                        ) : null}
                      </div>
                    </td>

                    <td className={CELL}>
                      <CellLabel>Model</CellLabel>
                      <span className="text-right font-mono text-[11px] text-muted-foreground md:text-left">
                        {agent.model || "the fleet default"}
                      </span>
                    </td>

                    <td className={CELL}>
                      <CellLabel>Delivery</CellLabel>
                      <Badge variant="outline" className="text-muted-foreground">
                        {agent.delivery || "none"}
                      </Badge>
                    </td>

                    <td className={CELL}>
                      <CellLabel>Actions</CellLabel>
                      <div className="flex flex-col items-end gap-1.5 md:items-start">
                        {canWrite ? (
                          <div className="flex flex-wrap items-center justify-end gap-1.5 md:justify-start">
                            <Button
                              variant="outline"
                              size="xs"
                              data-testid={`agent-open-${agent.id}`}
                              onClick={() => setPanel({ agentId: agent.id })}
                            >
                              Edit
                            </Button>
                            <Button
                              variant="outline"
                              size="xs"
                              disabled={busyRow === agent.id}
                              data-testid={`agent-toggle-${agent.id}`}
                              onClick={() => void toggle(agent)}
                            >
                              {agent.enabled ? <Pause aria-hidden /> : <Play aria-hidden />}
                              {agent.enabled ? "Disable" : "Enable"}
                            </Button>
                            <Button
                              variant="outline"
                              size="xs"
                              disabled={busyRow === agent.id}
                              data-testid={`agent-run-${agent.id}`}
                              onClick={() => void runNow(agent)}
                            >
                              <Rocket aria-hidden />
                              Run now
                            </Button>
                            <Button
                              variant="ghost"
                              size="xs"
                              data-testid={`agent-retire-${agent.id}`}
                              onClick={() =>
                                setRetiring(confirming ? null : { id: agent.id, typed: "" })
                              }
                            >
                              Retire
                            </Button>
                          </div>
                        ) : null}

                        {confirming ? (
                          <div
                            role="alertdialog"
                            aria-label={`Retire ${agent.id}?`}
                            className="flex w-full max-w-sm flex-col gap-1.5 rounded-md border border-border bg-background p-2.5"
                          >
                            <span className="text-xs text-foreground">
                              Retiring moves this manifest out of the fleet. Type{" "}
                              <span className="font-mono">{agent.id}</span> to confirm.
                            </span>
                            <Input
                              aria-label={`Type ${agent.id} to confirm`}
                              data-testid={`agent-retire-input-${agent.id}`}
                              value={retiring.typed}
                              autoComplete="off"
                              spellCheck={false}
                              onChange={(event) =>
                                setRetiring({ id: agent.id, typed: event.target.value })
                              }
                            />
                            <div className="flex flex-wrap items-center gap-1.5">
                              <Button
                                variant="destructive"
                                size="xs"
                                data-testid={`agent-retire-confirm-${agent.id}`}
                                disabled={retiring.typed !== agent.id || busyRow === agent.id}
                                onClick={() => void retire(agent.id)}
                              >
                                Retire agent
                              </Button>
                              <Button
                                variant="ghost"
                                size="xs"
                                data-testid={`agent-retire-cancel-${agent.id}`}
                                onClick={() => setRetiring(null)}
                              >
                                Keep it
                              </Button>
                            </div>
                          </div>
                        ) : null}

                        {rowNote ? (
                          <span
                            aria-live="polite"
                            className="text-xs text-muted-foreground"
                            data-testid={`agent-note-${agent.id}`}
                          >
                            {rowNote}
                          </span>
                        ) : null}
                        {rowReconcile[agent.id] ? (
                          <span
                            className="text-xs text-warning"
                            data-testid={`agent-reconcile-${agent.id}`}
                          >
                            {rowReconcile[agent.id]}
                          </span>
                        ) : null}
                        {rowError ? (
                          <span
                            className="text-xs text-destructive"
                            data-testid={`agent-error-${agent.id}`}
                          >
                            {rowError}
                          </span>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {broken.length ? (
        <div
          data-testid="agent-manifests-broken"
          className="flex flex-col gap-2 rounded-lg border border-warning/30 bg-warning/5 p-4"
        >
          <p className="text-sm font-medium text-warning">
            {broken.length === 1
              ? "One manifest will not load"
              : `${broken.length} manifests will not load`}
          </p>
          <p className="text-xs text-muted-foreground">
            The engine skipped these, so the agents in them are not running and are not in the list
            above. Fix the file and they come back on the next scan.
          </p>
          <ul className="flex flex-col gap-1">
            {broken.map((failure, index) => (
              <li
                key={`${failure.id}-${index}`}
                data-testid={`agent-broken-${failure.id || index}`}
                className="flex flex-wrap items-center gap-2 text-xs"
              >
                <span className="font-mono text-foreground">
                  {failure.filename || failure.id || "an unnamed manifest"}
                </span>
                <Badge variant="outline" className="border-warning/30 bg-warning/10 text-warning">
                  {failure.error_type || "unknown error"}
                </Badge>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
