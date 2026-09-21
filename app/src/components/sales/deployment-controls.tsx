"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiFetch } from "@/lib/api/client";

const API = "/api/sales/deployment";
type Fleet = { release_id: string | null; platform_revision?: string | null; agents: string[]; workflows: string[]; plugins: { name: string; version: string }[] };
type Release = Fleet & { name: string; version: string; source_revision: string };
type Transition = { id: string; direction: string; status: string; source_release_id: string | null; target_release_id: string | null;
  actor: string; reason: string; target: Fleet; source: Fleet; created_at?: string };
type Status = { configured: boolean; settings_revision: number; selected_release_id: string | null;
  pending: Transition | null; history: Transition[]; rollback_candidate: Transition | null; control_busy: boolean;
  runtime: { ready: boolean; reason: string | null } };

async function request<T>(path = "", body?: object): Promise<T> {
  const response = await fetch(API + path, {
    headers: { "Content-Type": "application/json" }, cache: "no-store",
    ...(body !== undefined ? { method: "POST", body: JSON.stringify(body) } : {}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : typeof data.error === "string" ? data.error : `Request failed (${response.status})`);
  return data;
}

function FleetSummary({ fleet }: { fleet: Fleet }) {
  return <div className="space-y-2 text-sm">
    <p className="break-all font-mono">{fleet.release_id ?? "Empty fleet"}</p>
    {fleet.platform_revision && <p className="break-all text-muted-foreground">Required platform revision: {fleet.platform_revision}</p>}
    <details><summary className="cursor-pointer">{fleet.agents.length} agents · {fleet.workflows.length} workflows · {fleet.plugins.length} adapters</summary>
      <ul className="mt-2 space-y-1 pl-4 break-words">
        {fleet.agents.map((id) => <li key={`agent:${id}`}>Agent: {id}</li>)}
        {fleet.workflows.map((id) => <li key={`workflow:${id}`}>Workflow: {id}</li>)}
        {fleet.plugins.map((plugin) => <li key={`plugin:${plugin.name}`}>Adapter: {plugin.name} {plugin.version}</li>)}
      </ul>
    </details>
  </div>;
}

export function DeploymentControls({ onChanged }: { onChanged: () => void | Promise<void> }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [fingerprint, setFingerprint] = useState("");
  const [reason, setReason] = useState("");
  const [candidate, setCandidate] = useState<Release | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshRequired, setRefreshRequired] = useState(false);
  const [confirmed, setConfirmed] = useState<string | null>(null);
  const inspection = useRef(0);

  const load = useCallback(async () => {
    const current = await request<Status>();
    setStatus(current);
    return current;
  }, []);
  useEffect(() => {
    let active = true;
    request<Status>().then((current) => { if (active) setStatus(current); })
      .catch((e) => { if (active) setError(String(e)); });
    return () => { active = false; inspection.current += 1; };
  }, []);

  async function refresh() {
    setBusy("refresh"); setError(null);
    try {
      const current = await load();
      if (!current.control_busy) setRefreshRequired(false);
    } catch (e) { setError(String(e)); }
    finally { setBusy(null); }
  }
  async function inspect() {
    const sequence = ++inspection.current;
    setBusy("inspect"); setError(null); setCandidate(null);
    try {
      const found = await request<Release>(`/releases/${fingerprint.trim()}`);
      if (sequence === inspection.current) setCandidate(found);
    } catch (e) { if (sequence === inspection.current) setError(String(e)); }
    finally { setBusy(null); }
  }
  async function perform(work: () => Promise<string>) {
    setBusy("change"); setError(null); setConfirmed(null);
    let acknowledged = false;
    try {
      const message = await work();
      acknowledged = true;
      setConfirmed(message);
      await load();
      await onChanged();
    } catch (e) {
      setRefreshRequired(true);
      setError(`${acknowledged ? "Action confirmed, but the display could not refresh." : String(e)} Refresh deployment status before another change.`);
    } finally { setBusy(null); }
  }
  async function change(path: string, body: object) {
    await perform(async () => {
      const result = await request<Transition>(path, body);
      return `Deployment ${result.status}.`;
    });
  }
  async function initialize() {
    await perform(async () => {
      await apiFetch("/api/bridge/api/sales/settings", { method: "PATCH", body: JSON.stringify({
        research_enabled: false, enrichment_enabled: false, promotion_enabled: false, sending_enabled: false, outcomes_enabled: false,
      }) });
      return "Sales workspace initialized with integrations paused.";
    });
  }

  const disabled = !!busy || !!status?.control_busy || refreshRequired;
  const reasonValid = reason.trim().length >= 10 && reason.trim().length <= 2000;
  const inspected = candidate?.release_id === fingerprint.trim();
  return <section aria-label="Sales deployment" className="rounded-lg border p-4 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h3 className="font-medium">Sales deployment</h3>
      <Button variant="outline" disabled={!!busy} onClick={() => void refresh()}>Refresh deployment status</Button>
    </div>
    <p className="text-sm text-muted-foreground">Installation and rollback pause all sales integrations. Enable them separately after verification. Every outbound message still needs individual approval.</p>
    {error && <p role="alert" className="rounded border border-destructive p-3 text-sm text-destructive">{error}</p>}
    {confirmed && <p role="status" className="text-sm">{confirmed}</p>}
    {!status ? <p>Loading deployment status…</p> : <>
      <div className="space-y-1 text-sm">
        <p>{status.selected_release_id ? "Selected fleet" : "No fleet selected"}</p>
        {status.selected_release_id && <p className="font-mono break-all">{status.selected_release_id}</p>}
        <p>{status.runtime.ready ? "Runtime verified" : "Runtime verification incomplete"}</p>
        {status.runtime.reason && <p className="text-muted-foreground">{status.runtime.reason}</p>}
        {status.control_busy && <p>A deployment operation is running. Refresh to check its outcome.</p>}
      </div>
      <label className="block space-y-1 text-sm" htmlFor="sales-deployment-reason">
        <span>Reason for this change</span>
        <textarea id="sales-deployment-reason" className="block min-h-20 w-full rounded border bg-transparent p-2" maxLength={2000}
          value={reason} onChange={(event) => setReason(event.target.value)} disabled={!!busy} />
      </label>
      {status.pending ? <article aria-label="Pending deployment" className="rounded border p-3 space-y-3">
        <h4 className="font-medium">{status.pending.direction === "rollback" ? "Pending rollback" : "Pending installation"}</h4>
        <p className="text-sm">Prepared by {status.pending.actor}: {status.pending.reason}</p>
        <FleetSummary fleet={status.pending.target} />
        <p className="text-sm text-muted-foreground">Commit verifies the installed platform, adapters and schedules. If verification fails, the transition stays pending and sales work remains paused.</p>
        <div className="flex flex-wrap gap-2">
          <Button disabled={disabled} onClick={() => void change(`/transitions/${status.pending!.id}/commit`, {})}>
            {status.pending.direction === "rollback" ? "Apply reviewed rollback" : "Install reviewed release"}
          </Button>
          <Button variant="outline" disabled={disabled || !reasonValid} onClick={() => void change(`/transitions/${status.pending!.id}/abort`, { reason: reason.trim() })}>Restore and cancel</Button>
        </div>
        <details><summary className="text-sm cursor-pointer">Restoration target</summary><FleetSummary fleet={status.pending.source} /></details>
      </article> : <>
        {!status.configured && <div className="space-y-2">
          <p className="text-sm">Initialize the sales workspace with all integrations paused before preparing an installation.</p>
          <Button variant="outline" disabled={disabled} onClick={() => void initialize()}>Initialize paused workspace</Button>
        </div>}
        <div className="space-y-3">
          <label htmlFor="sales-release-fingerprint" className="block space-y-1 text-sm"><span>Release fingerprint</span>
            <Input id="sales-release-fingerprint" value={fingerprint} maxLength={64} disabled={!!busy}
              onChange={(event) => { inspection.current += 1; setFingerprint(event.target.value); setCandidate(null); }} />
          </label>
          <Button variant="outline" disabled={disabled || !/^[a-f0-9]{64}$/.test(fingerprint.trim())} onClick={() => void inspect()}>Inspect release</Button>
          {candidate && inspected && <article aria-label="Inspected release" className="rounded border p-3 space-y-2">
            <h4 className="font-medium">{candidate.name}</h4><p className="text-sm">{candidate.version}</p>
            <FleetSummary fleet={candidate} />
            <p className="text-sm text-muted-foreground break-all">Agent and Sales Brain source revision: {candidate.source_revision}</p>
          </article>}
          <Button disabled={disabled || !status.configured || !inspected || !reasonValid}
            onClick={() => void change("/prepare", { release_id: candidate!.release_id, expected_revision: status.settings_revision, reason: reason.trim() })}>Prepare installation</Button>
        </div>
        {status.rollback_candidate && <article className="rounded border p-3 space-y-3" aria-label="Previous fleet">
          <h4 className="font-medium">Previous fleet</h4><FleetSummary fleet={status.rollback_candidate.source} />
          <Button variant="outline" disabled={disabled || !reasonValid} onClick={() => void change(`/transitions/${status.rollback_candidate!.id}/rollback`, {
            expected_revision: status.settings_revision, reason: reason.trim(),
          })}>Prepare rollback</Button>
        </article>}
      </>}
      {!!status.history.length && <details><summary className="cursor-pointer text-sm">Recent deployment history (up to 20)</summary>
        <ol className="mt-2 space-y-3 text-sm">{status.history.map((item) => <li key={item.id} className="rounded border p-2 space-y-1">
          <p>{item.direction} · {item.status}</p><p className="break-all font-mono">{item.target_release_id ?? "Empty fleet"}</p>
          <p>Prepared by {item.actor}: {item.reason}</p>
          {item.created_at && <p className="text-muted-foreground">{new Date(item.created_at).toLocaleString()}</p>}
        </li>)}</ol>
      </details>}
    </>}
  </section>;
}
