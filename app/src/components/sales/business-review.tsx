"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import { ApiError, apiFetch } from "@/lib/api/client";
import { useSalesPages } from "./use-sales-pages";

type Practice = { id: string; source: string; account_id: string; external_id: string; revision: string;
  observed_at: string; prospect_id: string | null; binding_status: string | null; review_reason?: string;
  data: { name: string; state: string | null; active: boolean; business_unit_id: string } };
type Customer = { id: string; name: string; domain: string };
export type BusinessSource = { source: string; account_id: string };
const API = "/api/bridge/api/sales";

export function BusinessReview({ prospects, sources, onChanged }: {
  prospects: Customer[]; sources: BusinessSource[]; onChanged: () => Promise<void>;
}) {
  const [filter, setFilter] = useState("");
  const source = sources.find((item) => `${item.source}:${item.account_id}` === filter);
  const query = new URLSearchParams({ kind: "practice" });
  if (source) { query.set("source", source.source); query.set("account_id", source.account_id); }
  const pages = useSalesPages<Practice>(`${API}/business-observations?${query}`);
  const [selected, setSelected] = useState<Practice | null>(null);
  const [customer, setCustomer] = useState("");
  const [reason, setReason] = useState("");
  const [verified, setVerified] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const different = !!selected?.prospect_id && selected.prospect_id !== customer;
  const target = prospects.find((p) => p.id === customer);

  function review(row: Practice) {
    setSelected(row); setCustomer(""); setReason(""); setVerified(false); setNotice(null); setError(null);
  }
  async function save() {
    if (!selected || !target || different || !verified || reason.trim().length < 10 || busy) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      await apiFetch(`${API}/prospects/${encodeURIComponent(target.id)}/business-customer`, {
        method: "POST", body: JSON.stringify({ observation_id: selected.id, expected_revision: selected.revision, reason: reason.trim() }),
      });
      setNotice("Practice match saved."); setSelected(null);
      await pages.reload();
      try { await onChanged(); } catch { setError("Match saved; refresh the workspace to load the updated customer."); }
    } catch (e) {
      setSelected(null);
      setError(e instanceof ApiError && e.status === 409
        ? "The practice or its match changed. Refresh practices and review again."
        : `Could not save the match. ${String(e)}`);
    } finally { setBusy(false); }
  }

  return <section aria-label="Imported practice review" className="space-y-3 rounded-lg border p-4">
    <h3 className="font-medium">Match imported practices to customers</h3>
    <p className="text-sm text-muted-foreground">Review the business identity and location before linking. One customer can own several practices. Order imports start only for reviewed matches.</p>
    <div className="flex flex-wrap items-center gap-3">
      <label>Practice source <NativeSelect aria-label="Practice source" disabled={busy} value={filter}
        onChange={(e) => { setFilter(e.target.value); setSelected(null); setError(null); setNotice(null); }}>
        <option value="">All imported sources</option>
        {sources.map((item) => <option key={`${item.source}:${item.account_id}`} value={`${item.source}:${item.account_id}`}>{item.source} · {item.account_id}</option>)}
      </NativeSelect></label>
      <Button variant="outline" disabled={busy || pages.loading} onClick={() => { setSelected(null); setError(null); void pages.reload(); }}>Refresh practices</Button>
    </div>
    {(error || pages.error) && <p role="alert" className="text-destructive">{error || pages.error}</p>}
    {notice && <p role="status">{notice}</p>}
    {pages.loading && <p>Loading practices…</p>}
    {!pages.loading && !pages.error && !pages.items.length && <p>No imported practices in this selection.</p>}
    <div className="grid gap-3 md:grid-cols-2">{pages.items.map((row) => <article key={row.id} className="rounded border p-3 space-y-2 text-sm">
      <h4 className="font-medium">{row.data.name}</h4>
      <p>{row.data.state ?? "State unknown"} · {row.data.active ? "Active" : "Inactive"}</p>
      <p>Source: {row.source} · Account: {row.account_id}</p>
      <p className="break-all">Practice: {row.external_id} · Business group: {row.data.business_unit_id}</p>
      <p>Observed: {new Date(row.observed_at).toLocaleString()}</p>
      <p>{row.binding_status === "held" ? "Match held for identity review" : row.prospect_id ? "Matched customer" : "Not matched"}{row.prospect_id ? `: ${prospects.find((p) => p.id === row.prospect_id)?.name ?? row.prospect_id}` : ""}</p>
      {row.review_reason && <p>Previous review: {row.review_reason}</p>}
      <Button variant="outline" disabled={busy || pages.loading} aria-label={`Review match for ${row.data.name}`} onClick={() => review(row)}>Review match</Button>
    </article>)}</div>
    {pages.next_cursor && <Button variant="outline" disabled={busy || pages.loading} onClick={() => void pages.more()}>Load more practices</Button>}
    {selected && <form className="rounded border p-4 space-y-3" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <h4 className="font-medium">Review {selected.data.name}</h4>
      <label className="block">Genus customer <NativeSelect aria-label="Genus customer" value={customer} disabled={busy}
        onChange={(e) => { setCustomer(e.target.value); setVerified(false); }}>
        <option value="">Select a customer</option>
        {prospects.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.domain}</option>)}
      </NativeSelect></label>
      {target && <p>Link {selected.data.name} ({selected.external_id}) to {target.name} ({target.domain}).</p>}
      {different && <p>This practice is already matched to another customer. Reassignment requires a separate identity repair.</p>}
      <label className="block">Matching evidence and reason
        <textarea className="mt-1 block w-full rounded border bg-background p-2" value={reason} minLength={10} maxLength={2000} required
          disabled={busy} onChange={(e) => setReason(e.target.value)} />
      </label>
      <label className="flex items-center gap-2"><input type="checkbox" checked={verified} disabled={busy} onChange={(e) => setVerified(e.target.checked)} />
        I verified that this practice belongs to this customer
      </label>
      <div className="flex gap-2"><Button type="submit" disabled={busy || !target || different || !verified || reason.trim().length < 10}>Confirm practice match</Button>
        <Button type="button" variant="outline" disabled={busy} onClick={() => setSelected(null)}>Cancel review</Button></div>
    </form>}
  </section>;
}
