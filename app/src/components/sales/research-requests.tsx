"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiFetch } from "@/lib/api/client";

const API = "/api/bridge/api/sales/requests";
type Brief = { request_key: string; title: string; query: string; buying_case: string; target_companies: number };
type Item = { id: string; revision: number; status: string; config: Brief };
type Detail = Item & { progress: { phase: string; discovered: number; assessed: number; remaining: number; failed_jobs: number; spent_units: number; reserved_units: number }; jobs: { id: string; kind: string; status: string; error?: string }[] };
type Page = { items: Item[]; next_cursor: string | null };

export function ResearchRequests({ buyingCases }: { buyingCases: string[] }) {
  const [page, setPage] = useState<Page | null>(null);
  const [selected, setSelected] = useState<Detail | null>(null);
  const [title, setTitle] = useState("");
  const [query, setQuery] = useState("");
  const [target, setTarget] = useState("100");
  const [buyingCase, setBuyingCase] = useState(buyingCases[0] ?? "");
  const [pending, setPending] = useState<Brief | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [held, setHeld] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selection = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    let active = true;
    mounted.current = true;
    apiFetch<Page>(API).then((data) => { if (active) setPage(data); }).catch((e) => { if (active) setError(String(e)); });
    return () => { active = false; mounted.current = false; };
  }, []);

  async function open(id: string) {
    const token = ++selection.current;
    setError(null); setBusy(true);
    try {
      const data = await apiFetch<Detail>(`${API}/${id}`);
      if (mounted.current && selection.current === token) { setSelected(data); setHeld(false); setReason(""); }
    } catch (e) { if (mounted.current && selection.current === token) setError(String(e)); }
    finally { if (mounted.current && selection.current === token) setBusy(false); }
  }
  async function create() {
    setError(null);
    let body = pending;
    if (!body) {
      if (!title.trim() || query.trim().length < 10 || !buyingCases.includes(buyingCase) || !/^\d+$/.test(target) || Number(target) < 1 || Number(target) > 1000) {
        setError("Enter a title, a business brief of at least 10 characters, an active buying case and 1–1,000 new companies."); return;
      }
      body = { request_key: crypto.randomUUID(), title: title.trim(), query: query.trim(), buying_case: buyingCase, target_companies: Number(target) };
      setPending(body);
    }
    setBusy(true);
    try {
      const created = await apiFetch<Item>(API, { method: "POST", body: JSON.stringify(body) });
      setPending(null); setTitle(""); setQuery("");
      setPage((previous) => ({ items: [created, ...(previous?.items ?? []).filter((r) => r.id !== created.id)], next_cursor: previous?.next_cursor ?? null }));
      await open(created.id);
    } catch (e) { setError(`${String(e)} Retry uses the same request identity and brief.`); }
    finally { setBusy(false); }
  }
  async function change(status: string) {
    if (!selected) return;
    if (reason.trim().length < 10) { setError("Explain the change in at least 10 characters."); return; }
    setBusy(true); setError(null);
    try {
      const updated = await apiFetch<Detail>(`${API}/${selected.id}/state`, { method: "POST", body: JSON.stringify({ status, expected_revision: selected.revision, reason: reason.trim() }) });
      setSelected(updated); setReason("");
      setPage((previous) => previous && ({ ...previous, items: previous.items.map((r) => r.id === updated.id ? updated : r) }));
    } catch (e) { setError(`${String(e)} Reload this request before another change.`); setHeld(true); }
    finally { setBusy(false); }
  }
  async function more() {
    if (!page?.next_cursor) return;
    setBusy(true); setError(null);
    try {
      const next = await apiFetch<Page>(`${API}?after=${encodeURIComponent(page.next_cursor)}`);
      setPage((previous) => ({ items: [...(previous?.items ?? []), ...next.items.filter((r) => !previous?.items.some((p) => p.id === r.id))], next_cursor: next.next_cursor }));
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  return <section aria-label="Research requests" className="rounded-lg border p-4 space-y-4">
    <h3 className="font-medium">Give the sales fleet a research task</h3>
    <p className="text-sm text-muted-foreground">Describe the businesses and set a target of new companies to research. The fleet uses approved qualification policies and existing spending, schedule and review limits. Creating a request never enables sending or approves a message.</p>
    {error && <p role="alert">{error}</p>}
    {!buyingCases.length && <p>Publish and select a qualification policy before creating a request.</p>}
    <div className="grid gap-3 md:grid-cols-2">
      <label>Request title<Input value={title} maxLength={200} disabled={busy || !!pending} onChange={(e) => setTitle(e.target.value)} /></label>
      <label>New companies to research<Input value={target} inputMode="numeric" disabled={busy || !!pending} onChange={(e) => setTarget(e.target.value)} /></label>
      <label>Buying case<select className="block w-full rounded border p-2" value={buyingCase} disabled={busy || !!pending} onChange={(e) => setBuyingCase(e.target.value)}>{buyingCases.map((c) => <option key={c} value={c}>{c}</option>)}</select></label>
      <label>Businesses to find<textarea className="block w-full rounded border p-2" maxLength={4000} value={query} disabled={busy || !!pending} onChange={(e) => setQuery(e.target.value)} /></label>
    </div>
    <Button disabled={busy || !buyingCases.length} onClick={() => void create()}>{pending ? "Retry the same request" : "Create research request"}</Button>
    {!page ? <p>Loading research requests…</p> : !page.items.length ? <p>No research requests yet.</p> : <div className="space-y-2">{page.items.map((r) => <Button key={r.id} variant="outline" disabled={busy} onClick={() => void open(r.id)}>{r.config.title} · {r.config.target_companies} companies · {r.status}</Button>)}</div>}
    {page?.next_cursor && <Button variant="outline" disabled={busy} onClick={() => void more()}>Load more requests</Button>}
    {selected && <article aria-label="Request progress" className="rounded border p-3 space-y-3">
      <h4 className="font-medium">{selected.config.title}</h4><p>{selected.config.query}</p>
      <p>{selected.progress.phase} · {selected.progress.discovered} discovered · {selected.progress.assessed} assessed · {selected.progress.remaining} remaining</p>
      <p>Accounted usage: ${(selected.progress.spent_units / 1e6).toFixed(2)} · Reserved: ${(selected.progress.reserved_units / 1e6).toFixed(2)}. These figures are not reconciled invoices.</p>
      <p className="text-sm">Research completion means the company target has been assessed. Lead acceptance, contact checks and individual message approval remain separate.</p>
      {selected.jobs.filter((j) => j.error || j.status === "failed").map((j) => <p key={j.id} className="text-sm">{j.kind}: {j.status} · {j.error}</p>)}
      <Button variant="outline" disabled={busy} onClick={() => void open(selected.id)}>Reload this request</Button>
      {selected.status !== "cancelled" && <>
        <p className="text-sm">Pausing holds new automated work and cancels pending message approvals. Resuming does not restore those approvals. Cancellation is permanent.</p>
        <label>Reason for request change<textarea className="block w-full rounded border p-2" maxLength={2000} disabled={busy || held} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
        <Button variant="outline" disabled={busy || held} onClick={() => void change(selected.status === "active" ? "paused" : "active")}>{selected.status === "active" ? "Pause this request" : "Resume this request"}</Button>
        <Button variant="outline" disabled={busy || held} onClick={() => void change("cancelled")}>Cancel this request</Button>
      </>}
    </article>}
  </section>;
}
