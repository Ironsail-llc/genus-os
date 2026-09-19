"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import { ApiError, apiFetch } from "@/lib/api/client";
import { useSalesPages } from "./use-sales-pages";

type ReadJob = { id: string; kind: string; status: string; error: string | null; attempts: number; max_attempts: number;
  updated_at: string; scope: Record<string, string> };
const API = "/api/bridge/api/sales";
const names: Record<string, string> = { "sales.gmail_sync": "Gmail conversation", "sales.provider_status": "Provider status", "sales.business": "Business import", "sales.inbound": "Incoming message", "sales.reconcile": "Message reconciliation" };

export function ReadRecovery() {
  const [state, setState] = useState("attention");
  const [kind, setKind] = useState("");
  const query = new URLSearchParams({ state });
  if (kind) query.set("kind", kind);
  const pages = useSalesPages<ReadJob>(`${API}/provider-reads?${query}`);
  const [selected, setSelected] = useState<ReadJob | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  async function retry() {
    if (!selected || busy || reason.trim().length < 10) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      await apiFetch(`${API}/jobs/${encodeURIComponent(selected.id)}/retry`, { method: "POST", body: JSON.stringify({ reason: reason.trim() }) });
      setSelected(null); setNotice("Read queued for retry."); await pages.reload();
    } catch (e) {
      setSelected(null); setError(e instanceof ApiError && e.status === 409
        ? "This read changed or is already running. Refresh reads before retrying."
        : `Could not queue this read. ${String(e)}`);
    } finally { setBusy(false); }
  }
  return <section className="space-y-3 rounded-lg border p-4" aria-label="Provider read recovery">
    <h3 className="font-medium">Provider read recovery</h3>
    <p className="text-sm text-muted-foreground">Inspect the source and repair its configuration before retrying. A retry resumes the same read page; it does not repeat an email send or CRM write.</p>
    <div className="flex flex-wrap gap-3">
      <label>Read status <NativeSelect aria-label="Read status" disabled={busy} value={state} onChange={(e) => { setState(e.target.value); setSelected(null); }}>
        <option value="attention">Needs attention</option><option value="all">All reads</option>
      </NativeSelect></label>
      <label>Read type <NativeSelect aria-label="Read type" disabled={busy} value={kind} onChange={(e) => { setKind(e.target.value); setSelected(null); }}>
        <option value="">All types</option>{Object.entries(names).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
      </NativeSelect></label>
      <Button variant="outline" disabled={busy || pages.loading} onClick={() => { setSelected(null); setError(null); void pages.reload(); }}>Refresh reads</Button>
    </div>
    {(error || pages.error) && <p role="alert" className="text-destructive">{error || pages.error}</p>}
    {notice && <p role="status">{notice}</p>}
    {pages.loading && <p>Loading reads…</p>}
    {!pages.loading && !pages.error && !pages.items.length && <p>No reads in this selection.</p>}
    {pages.items.map((job) => <article key={job.id} className="rounded border p-3 space-y-2 text-sm">
      <p className="font-medium">{names[job.kind] ?? job.kind} · <span>{job.status}</span></p>
      <p>{Object.entries(job.scope).map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`).join(" · ")}</p>
      <p>Attempts: {job.attempts}/{job.max_attempts} · Updated: {new Date(job.updated_at).toLocaleString()}</p>
      {job.error && <p>{job.error}</p>}
      {["pending", "failed"].includes(job.status) && <Button variant="outline" disabled={busy || pages.loading}
        onClick={() => { setSelected(job); setReason(""); setError(null); setNotice(null); }}>Review read recovery</Button>}
    </article>)}
    {pages.next_cursor && <Button variant="outline" disabled={busy || pages.loading} onClick={() => void pages.more()}>Load more reads</Button>}
    {selected && <form className="space-y-3 rounded border p-4" onSubmit={(e) => { e.preventDefault(); void retry(); }}>
      <h4 className="font-medium">Retry {names[selected.kind] ?? selected.kind}</h4>
      <p className="break-all text-sm">Read reference: {selected.id}</p>
      <label className="block">What was repaired?<textarea className="mt-1 block w-full rounded border bg-background p-2" value={reason}
        minLength={10} maxLength={2000} required disabled={busy} onChange={(e) => setReason(e.target.value)} /></label>
      <div className="flex gap-2"><Button type="submit" disabled={busy || reason.trim().length < 10}>Retry this read</Button>
        <Button type="button" variant="outline" disabled={busy} onClick={() => setSelected(null)}>Cancel retry</Button></div>
    </form>}
  </section>;
}
