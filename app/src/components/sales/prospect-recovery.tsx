"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { apiFetch } from "@/lib/api/client";

type Snapshot = { state_hash: string; prospect: { owner: string; status: string }; jobs: { id: string; kind: string; status: string; error?: string }[]; reservations: { actual_units: number | null }[] };
const commands = [["resume", "Return ownership to Robothor"], ["research", "Research the company again"], ["qualify", "Reassess the current dossier"], ["contacts", "Search for contacts again"], ["initial", "Prepare a new initial draft"], ["reply", "Prepare a reply to the latest inbound message"], ["activation", "Reassess the confirmed customer milestone"]];

export function ProspectRecovery({ prospectId, onChanged }: { prospectId: string; onChanged: () => Promise<void> }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [command, setCommand] = useState("resume");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [held, setHeld] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const path = `/api/bridge/api/sales/prospects/${prospectId}/recovery`;
  async function load() {
    setBusy(true); setError(null);
    try { setSnapshot(await apiFetch<Snapshot>(path)); setHeld(false); setReason(""); }
    catch (e) { setError(String(e)); setHeld(true); }
    finally { setBusy(false); }
  }
  async function apply() {
    if (!snapshot || held || reason.trim().length < 10) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      await apiFetch(path, { method: "POST", body: JSON.stringify({ command, expected_hash: snapshot.state_hash, reason: reason.trim() }) });
      setHeld(true); setNotice("Recovery recorded. Old approvals remain cancelled. Reload before another decision.");
      await onChanged();
    } catch (e) { setError(`${String(e)} Reload before another decision.`); setHeld(true); }
    finally { setBusy(false); }
  }
  return <section className="space-y-3 rounded border p-3" aria-label="Preparation recovery">
    <Button variant="outline" disabled={busy} onClick={() => void load()}>{snapshot ? "Reload recovery state" : "Review preparation and ownership"}</Button>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {snapshot && <>
      <p>Current owner: <span>{snapshot.prospect.owner}</span> · {snapshot.prospect.status}</p>
      <p className="text-sm text-muted-foreground">Recovery cancels pending approvals and supersedes old preparation jobs. Return ownership first, then select the next preparation step. Fresh research or qualification withdraws lead acceptance. New drafts require individual approval; unknown delivery and unsettled spending must be resolved first.</p>
      {snapshot.jobs.filter((j) => j.error || j.status === "running").map((j) => <p key={j.id} className="text-sm">{j.kind}: {j.status} · {j.error}</p>)}
      <label className="block">Recovery command<select className="block rounded border bg-background p-2" value={command} disabled={busy || held} onChange={(e) => setCommand(e.target.value)}>{commands.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="block">Reason and preparation instructions<textarea className="block w-full rounded border bg-background p-2" minLength={10} maxLength={2000} value={reason} disabled={busy || held} onChange={(e) => setReason(e.target.value)} /></label>
      <Button disabled={busy || held || reason.trim().length < 10} onClick={() => void apply()}>Apply reviewed recovery</Button>
    </>}
  </section>;
}
