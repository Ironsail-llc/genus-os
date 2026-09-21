"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiError, apiFetch } from "@/lib/api/client";

type Proof = { content_hash: string; receipt: { id: string; delivery_status: string };
  message: { sender: string; recipient: string; subject: string; body: string; occurred_at: string } };

export function GmailRecovery({ actionId, onChanged }: { actionId: string; onChanged: () => Promise<void> }) {
  const [proof, setProof] = useState<Proof | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const path = `/api/bridge/api/sales/actions/${encodeURIComponent(actionId)}/gmail`;
  async function inspect() {
    setBusy(true); setProof(null); setReason(""); setError(null); setNotice(null);
    try { setProof(await apiFetch<Proof>(`${path}/inspect`, { method: "POST", body: "{}" })); }
    catch (e) { setError(`Could not establish a matching Gmail sent copy. ${String(e)}`); }
    finally { setBusy(false); }
  }
  async function reconcile() {
    if (!proof || busy || reason.trim().length < 10) return;
    setBusy(true); setError(null);
    try {
      await apiFetch(`${path}/reconcile`, { method: "POST", body: JSON.stringify({ expected_hash: proof.content_hash, reason: reason.trim() }) });
      setProof(null); setNotice("Sent copy reconciled. No message was resent."); await onChanged();
    } catch (e) {
      setProof(null); setError(e instanceof ApiError && e.status === 409
        ? "The action or evidence changed. Inspect the Gmail sent copy again before reviewing."
        : `Could not reconcile this action. Inspect it again. ${String(e)}`);
    } finally { setBusy(false); }
  }
  return <section aria-label="Gmail send recovery" className="space-y-3 rounded border p-3 text-sm">
    <p>Inspect Gmail for the exact approved message before resolving this held send. A sent copy does not confirm delivery to the recipient. This control never resends an email.</p>
    <Button variant="outline" disabled={busy} onClick={() => void inspect()}>Inspect Gmail sent copy</Button>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {proof && <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); void reconcile(); }}>
      <p>{proof.message.sender} → {proof.message.recipient}</p>
      <p>{proof.message.subject} · {new Date(proof.message.occurred_at).toLocaleString()}</p>
      <p className="whitespace-pre-wrap">{proof.message.body}</p>
      <p className="break-all">Gmail reference: {proof.receipt.id}</p>
      <label className="block">Gmail recovery review reason<textarea className="mt-1 block w-full rounded border bg-background p-2"
        value={reason} onChange={(e) => setReason(e.target.value)} minLength={10} maxLength={2000} required disabled={busy} /></label>
      <Button disabled={busy || reason.trim().length < 10} type="submit">Reconcile this sent message</Button>
    </form>}
  </section>;
}
