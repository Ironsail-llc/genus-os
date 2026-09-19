"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { apiFetch } from "@/lib/api/client";

const API = "/api/bridge/api/sales";
type Kind = "qualification" | "knowledge";
type Entry = { kind: Kind; version: string; approved_by: string; approved_at: string; data: Record<string, unknown> };
type Page = { items: Entry[]; next_cursor: string | null };
type Config = { active_policy_versions?: Record<string, string>; active_knowledge_version?: string };
type Snapshot = { config: Config; revision: number };
type Selection = { policy_versions: Record<string, string>; knowledge_version: string; expected_revision: number; reason: string };
const label = (value: string) => value.replaceAll("_", " ");
const ordered = (value: Record<string, string>) => JSON.stringify(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)));

function PublishedEntry({ entry }: { entry: Entry }) {
  const data = entry.data;
  const weights = (data.weights ?? {}) as Record<string, number>;
  const required = (data.required ?? []) as string[];
  return <article className="rounded border p-3 space-y-2 text-sm">
    <h5 className="font-medium">{entry.kind === "knowledge" ? "Claims" : label(String(data.buying_case))} · {entry.version}</h5>
    <p className="text-muted-foreground">Published by {entry.approved_by} · {new Date(entry.approved_at).toLocaleDateString()}</p>
    {entry.kind === "qualification" ? <>
      <p>Qualification threshold: {String(data.threshold)} / 100 · Evidence age limit: {String(data.max_evidence_age_days)} days</p>
      <ul className="space-y-1">{Object.entries(weights).map(([key, points]) => <li key={key}>{label(key)}: {points} points{required.includes(key) ? " · required" : ""}</li>)}</ul>
    </> : <dl className="space-y-3">{Object.entries((data.claims ?? {}) as Record<string, unknown>).map(([key, claim]) => <div key={key}>
      <dt className="font-medium">{label(key)}</dt>
      <dd className="whitespace-pre-wrap break-words">{typeof claim === "string" ? claim : JSON.stringify(claim, null, 2)}</dd>
    </div>)}</dl>}
    {entry.kind === "knowledge" && Object.keys(data).some((key) => key !== "claims") && <details>
      <summary className="cursor-pointer">Published supporting details</summary>
      <pre className="mt-2 whitespace-pre-wrap break-words">{JSON.stringify(Object.fromEntries(Object.entries(data).filter(([key]) => key !== "claims")), null, 2)}</pre>
    </details>}
  </article>;
}

export function SalesLibrary({ onChanged }: { onChanged: () => void | Promise<void> }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [pages, setPages] = useState<Record<Kind, Page>>({ qualification: { items: [], next_cursor: null }, knowledge: { items: [], next_cursor: null } });
  const [policies, setPolicies] = useState<Record<string, string>>({});
  const [knowledge, setKnowledge] = useState("");
  const [reason, setReason] = useState("");
  const [review, setReview] = useState<Selection | null>(null);
  const [busy, setBusy] = useState(false);
  const [held, setHeld] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  function accept(current: Snapshot) {
    setSnapshot(current); setPolicies(current.config.active_policy_versions ?? {});
    setKnowledge(current.config.active_knowledge_version ?? "");
    setReview(null); setReason(""); setHeld(false);
  }
  async function read() {
    return Promise.all([apiFetch<Snapshot>(API + "/settings", { cache: "no-store" }),
      apiFetch<Page>(API + "/library?kind=qualification", { cache: "no-store" }),
      apiFetch<Page>(API + "/library?kind=knowledge", { cache: "no-store" })]);
  }
  useEffect(() => {
    let active = true;
    void read().then(([current, qualification, knowledge]) => { if (active) { accept(current); setPages({ qualification, knowledge }); } })
      .catch((e) => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, []);
  async function reload() {
    setBusy(true); setError(null); setSaved(false);
    try { const [current, qualification, knowledge] = await read(); accept(current); setPages({ qualification, knowledge }); }
    catch (e) { setError(String(e)); setHeld(true); }
    finally { setBusy(false); }
  }
  async function more(kind: Kind) {
    setBusy(true); setError(null);
    try {
      const next = await apiFetch<Page>(`${API}/library?kind=${kind}&after=${encodeURIComponent(pages[kind].next_cursor!)}`, { cache: "no-store" });
      setPages((current) => ({ ...current, [kind]: { items: [...current[kind].items, ...next.items.filter((item) => !current[kind].items.some((old) => old.version === item.version))], next_cursor: next.next_cursor } }));
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  const cases = [...new Set([...Object.keys(snapshot?.config.active_policy_versions ?? {}), ...pages.qualification.items.map((entry) => String(entry.data.buying_case))])].sort();
  const selected = pages.qualification.items.filter((entry) => policies[String(entry.data.buying_case)] === entry.version)
    .concat(pages.knowledge.items.filter((entry) => entry.version === knowledge));
  function inspect() {
    if (!snapshot) return;
    setError(null); setSaved(false); setReview(null);
    if (reason.trim().length < 10) return setError("Explain the library change in at least 10 characters.");
    if (ordered(policies) === ordered(snapshot.config.active_policy_versions ?? {}) && knowledge === (snapshot.config.active_knowledge_version ?? "")) return setError("Change at least one selected version before review.");
    if (selected.length !== Object.keys(policies).length + (knowledge ? 1 : 0)) return setError("Load the published record for every selected version before review. Missing or unpublished versions cannot be selected.");
    setReview({ policy_versions: { ...policies }, knowledge_version: knowledge, expected_revision: snapshot.revision, reason: reason.trim() });
  }
  async function save() {
    if (!review || held || busy) return;
    setBusy(true); setError(null);
    let acknowledged = false;
    try {
      const current = await apiFetch<Snapshot>(API + "/library/selection", { method: "POST", body: JSON.stringify(review) });
      acknowledged = true; accept(current); setSaved(true); await onChanged();
    } catch (e) {
      setHeld(true); setReview(null);
      setError(`${acknowledged ? "Library selection saved, but the workspace could not refresh." : String(e)} Reload the library and review again before another change.`);
    } finally { setBusy(false); }
  }
  return <section aria-label="Sales library" className="rounded-lg border p-4 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">Qualification policies and sales claims</h3>
      <Button variant="outline" disabled={busy} onClick={() => void reload()}>Reload library</Button></div>
    <p className="text-sm text-muted-foreground">Select published versions for each buying case and inspect the rules and claims before saving. A change retires earlier message approvals and queues pauses for older campaigns. Integration switches retain their current settings.</p>
    {error && <p role="alert" className="rounded border border-destructive p-3 text-sm text-destructive">{error}</p>}
    {saved && <p role="status" className="text-sm">Reviewed library selected.</p>}
    {!snapshot ? <p>Loading published library…</p> : <>
      {!cases.length && <p className="text-sm">No qualification policies have been published yet.</p>}
      {cases.map((buyingCase) => <label key={buyingCase} className="block space-y-1 text-sm"><span>Qualification for {label(buyingCase)}</span>
        <select className="block w-full rounded border bg-background p-2" disabled={busy || held} value={policies[buyingCase] ?? ""}
          onChange={(event) => { const next = { ...policies }; if (event.target.value) next[buyingCase] = event.target.value; else delete next[buyingCase]; setPolicies(next); setReview(null); setSaved(false); }}>
          <option value="">No active policy</option>
          {policies[buyingCase] && !pages.qualification.items.some((entry) => entry.version === policies[buyingCase] && entry.data.buying_case === buyingCase) && <option value={policies[buyingCase]}>{policies[buyingCase]} (record not loaded)</option>}
          {pages.qualification.items.filter((entry) => entry.data.buying_case === buyingCase).map((entry) => <option key={entry.version} value={entry.version}>{entry.version}</option>)}
        </select>
      </label>)}
      <label className="block space-y-1 text-sm"><span>Active claim library</span>
        <select className="block w-full rounded border bg-background p-2" disabled={busy || held} value={knowledge} onChange={(event) => { setKnowledge(event.target.value); setReview(null); setSaved(false); }}>
          <option value="">No active claims</option>
          {knowledge && !pages.knowledge.items.some((entry) => entry.version === knowledge) && <option value={knowledge}>{knowledge} (record not loaded)</option>}
          {pages.knowledge.items.map((entry) => <option key={entry.version} value={entry.version}>{entry.version}</option>)}
        </select>
      </label>
      {(["qualification", "knowledge"] as Kind[]).map((kind) => pages[kind].next_cursor && <Button key={kind} variant="outline" disabled={busy || held} onClick={() => void more(kind)}>Load more {kind} versions</Button>)}
      {!!selected.length && <details><summary className="cursor-pointer text-sm">Inspect selected records</summary>
        <div className="mt-3 space-y-3">{selected.map((entry) => <PublishedEntry key={entry.kind + ":" + entry.version} entry={entry} />)}</div>
      </details>}
      <label className="block space-y-1 text-sm"><span>Reason for library selection</span><textarea className="w-full rounded border bg-transparent p-2" maxLength={2000} disabled={busy || held} value={reason} onChange={(event) => { setReason(event.target.value); setReview(null); }} /></label>
      <Button variant="outline" disabled={busy || held} onClick={inspect}>Review library selection</Button>
      {review && <section aria-label="Review selected library" className="rounded border p-3 space-y-3">
        <h4 className="font-medium">Review selected library</h4>
        {cases.map((buyingCase) => <p key={buyingCase} className="text-sm">{label(buyingCase)}: {snapshot.config.active_policy_versions?.[buyingCase] ?? "None"} → {policies[buyingCase] ?? "None"}</p>)}
        <p className="text-sm">Claims: {snapshot.config.active_knowledge_version || "None"} → {knowledge || "None"}</p>
        {selected.map((entry) => <PublishedEntry key={entry.kind + ":" + entry.version} entry={entry} />)}
        <p className="text-sm">{review.reason}</p>
        <Button disabled={busy || held} onClick={() => void save()}>Use reviewed library</Button>
      </section>}
    </>}
  </section>;
}
