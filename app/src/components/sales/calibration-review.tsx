"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiFetch } from "@/lib/api/client";
import { LibraryEntry } from "./library-entry";

const API = "/api/bridge/api/sales/calibration";
type Cohort = { id: string; name: string; target_size: number; agreement_target_percent: number; policy_versions: Record<string, string> };
type Page<T> = { items: T[]; next_cursor: string | number | null };
type Summary = { enrolled: number; reviewed: number; agreements: number; agreement_percent: number | null; uncertain_reference: number;
  reference_qualified: number; reference_rejected: number; model_needs_research: number; false_positives: number; missed_fits: number; qualified_precision_percent: number | null };
type Report = { cohort: Cohort; initial: Summary; latest: Summary; corrections: number; review_complete: boolean; agreement_target_met: boolean;
  by_buying_case: Record<string, { policy_version: string; initial: Summary; latest: Summary }>; limitations: string[] };
type Assessment = { id: string; revision: number; reference_decision: string; reason: string; actor: string };
type ItemSummary = { id: string; ordinal: number; name: string; domain: string; buying_case: string; policy_version: string; snapshot_hash: string; reference_decision?: string | null };
type Item = ItemSummary & { snapshot: { name: string; domain: string; version: number; policy: Record<string, unknown>;
  qualification: { decision: string; score: number }; dossier: { summary: string; unanswered: string[]; evidence: { id: string; field: string; value: unknown; confidence: string; url: string; excerpt: string }[] } }; assessments: Assessment[] };
type Settings = { revision: number; config: { active_policy_versions?: Record<string, string> } };
const pct = (value: number | null) => value === null ? "Not measured" : `${value.toFixed(1)}%`;

function Metrics({ data }: { data: Summary }) {
  return <dl className="grid gap-2 text-sm sm:grid-cols-2">
    <div><dt>Reviewed dossiers</dt><dd>{data.reviewed} / {data.enrolled} enrolled</dd></div>
    <div><dt>Agreement</dt><dd>{pct(data.agreement_percent)}</dd></div>
    <div><dt>Human-qualified businesses</dt><dd>{data.reference_qualified}</dd></div>
    <div><dt>Human assessments needing research</dt><dd>{data.uncertain_reference}</dd></div>
    <div><dt>False positives</dt><dd>{data.false_positives}</dd></div>
    <div><dt>Missed fits, including model abstentions</dt><dd>{data.missed_fits}</dd></div>
    <div><dt>Precision among model-qualified, decisive reviews</dt><dd>{pct(data.qualified_precision_percent)}</dd></div>
  </dl>;
}

export function CalibrationReview() {
  const [cohorts, setCohorts] = useState<Page<Cohort>>({ items: [], next_cursor: null });
  const [settings, setSettings] = useState<Settings | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [items, setItems] = useState<Page<ItemSummary>>({ items: [], next_cursor: null });
  const [item, setItem] = useState<Item | null>(null);
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("");
  const [decision, setDecision] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [held, setHeld] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function index() {
    return Promise.all([apiFetch<Page<Cohort>>(API, { cache: "no-store" }), apiFetch<Settings>("/api/bridge/api/sales/settings", { cache: "no-store" })]);
  }
  useEffect(() => {
    let active = true;
    void index().then(([list, config]) => { if (active) { setCohorts(list); setSettings(config); } }).catch((e) => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, []);
  async function run(work: () => Promise<void>) {
    setBusy(true); setError(null); setMessage(null);
    try { await work(); }
    catch (e) { setHeld(true); setError(`${String(e)} Reload the cohort or review item before another change.`); }
    finally { setBusy(false); }
  }
  async function loadCohort(id: string) {
    const [current, page] = await Promise.all([apiFetch<Report>(`${API}/${id}`, { cache: "no-store" }), apiFetch<Page<ItemSummary>>(`${API}/${id}/items`, { cache: "no-store" })]);
    setReport(current); setItems(page); setItem(null); setDecision(""); setReason(""); setHeld(false);
  }
  async function loadItem(id: string) {
    const current = await apiFetch<Item>(`${API}/items/${id}`, { cache: "no-store" });
    setItem(current); setDecision(""); setReason(""); setHeld(false);
  }
  async function create() {
    if (!settings) return;
    await run(async () => {
      const current = await apiFetch<Cohort>(API, { method: "POST", body: JSON.stringify({ name: name.trim(), reason: purpose.trim(), target_size: 100, agreement_target_percent: 85, expected_settings_revision: settings.revision }) });
      const [list, config] = await index(); setCohorts(list); setSettings(config);
      await loadCohort(current.id); setName(""); setPurpose(""); setMessage("Review cohort created. Enroll eligible dossiers when ready.");
    });
  }
  async function assess() {
    if (!item || !report) return;
    await run(async () => {
      await apiFetch(`${API}/items/${item.id}/assessment`, { method: "POST", body: JSON.stringify({ expected_snapshot_hash: item.snapshot_hash,
        expected_assessment_id: item.assessments.at(-1)?.id ?? null, reference_decision: decision, reason: reason.trim() }) });
      const [current, updated] = await Promise.all([apiFetch<Item>(`${API}/items/${item.id}`, { cache: "no-store" }), apiFetch<Report>(`${API}/${report.cohort.id}`, { cache: "no-store" })]);
      setItem(current); setReport(updated); setDecision(""); setReason(""); setHeld(false);
      setItems((page) => ({ ...page, items: page.items.map((row) => row.id === current.id ? { ...row, reference_decision: current.assessments.at(-1)?.reference_decision } : row) }));
      setMessage("Assessment recorded. Promotion and message approvals are separate decisions.");
    });
  }
  return <section aria-label="Qualification review" className="rounded-lg border p-4 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">Qualification review cohorts</h3>
      <Button variant="outline" disabled={busy} onClick={() => void run(async () => { const [list, config] = await index(); setCohorts(list); setSettings(config); setHeld(false); if (report) await loadCohort(report.cohort.id); })}>Reload review cohorts</Button></div>
    <p className="text-sm text-muted-foreground">Each cohort freezes its policy versions and enrolled dossiers. Assess fit independently; the model result appears after your first assessment. Corrections preserve the original result. Customer conversion and delivery evidence are tracked separately.</p>
    {error && <p role="alert" className="rounded border border-destructive p-3 text-sm text-destructive">{error}</p>}
    {message && <p role="status" className="text-sm">{message}</p>}
    <details><summary className="cursor-pointer">Create a pilot review cohort</summary><div className="mt-3 space-y-3">
      <p className="text-sm">100 distinct dossiers · 85% initial agreement target · unresolved human assessments keep the review incomplete.</p>
      <ul className="text-sm">{Object.entries(settings?.config.active_policy_versions ?? {}).map(([key, value]) => <li key={key}>{key.replaceAll("_", " ")}: {value}</li>)}</ul>
      <label className="block space-y-1 text-sm"><span>Review cohort name</span><Input maxLength={200} value={name} disabled={busy || held} onChange={(event) => setName(event.target.value)} /></label>
      <label className="block space-y-1 text-sm"><span>Cohort review purpose</span><textarea className="w-full rounded border bg-transparent p-2" maxLength={2000} value={purpose} disabled={busy || held} onChange={(event) => setPurpose(event.target.value)} /></label>
      <Button disabled={busy || held || !name.trim() || purpose.trim().length < 10 || !Object.keys(settings?.config.active_policy_versions ?? {}).length} onClick={() => void create()}>Create 100-dossier cohort</Button>
    </div></details>
    <div className="flex flex-wrap gap-2">{cohorts.items.map((row) => <Button variant="outline" key={row.id} disabled={busy} onClick={() => void run(() => loadCohort(row.id))}>Open {row.name}</Button>)}</div>
    {cohorts.next_cursor && <Button variant="outline" disabled={busy} onClick={() => void run(async () => { const page = await apiFetch<Page<Cohort>>(`${API}?after=${encodeURIComponent(cohorts.next_cursor!)}`); setCohorts({ items: [...cohorts.items, ...page.items], next_cursor: page.next_cursor }); })}>More review cohorts</Button>}
    {report && <>
      <h4 className="font-medium">{report.cohort.name} · {report.cohort.target_size} dossier target</h4>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" disabled={busy || held || report.initial.enrolled >= report.cohort.target_size} onClick={() => void run(async () => { await apiFetch(`${API}/${report.cohort.id}/enroll`, { method: "POST", body: "{}" }); await loadCohort(report.cohort.id); })}>Enroll next eligible dossiers</Button>
        <Button variant="outline" disabled={busy} onClick={() => void run(() => loadCohort(report.cohort.id))}>Reload cohort</Button>
      </div>
      <p className="text-sm text-muted-foreground">Enrollment chooses the oldest currently eligible dossiers under this cohort’s policies, including qualified, rejected and incomplete model decisions. Membership and snapshots remain fixed.</p>
      <h5 className="font-medium">Original assessments</h5><Metrics data={report.initial} />
      <p className="text-sm">{report.review_complete ? "Reference review complete." : "Reference review incomplete."} {report.agreement_target_met ? "Initial agreement target met." : "Initial agreement target not yet met."}</p>
      {!!report.corrections && <details><summary className="cursor-pointer">Latest assessments · {report.corrections} corrected dossiers</summary><Metrics data={report.latest} /></details>}
      <details><summary className="cursor-pointer">Results by buying case and sampling limits</summary><div className="mt-3 space-y-4">
        {Object.entries(report.by_buying_case).map(([key, group]) => <div key={key}><h5 className="text-sm font-medium">{key.replaceAll("_", " ")} · {group.policy_version}</h5><Metrics data={group.initial} /></div>)}
        <ul className="space-y-1 text-sm text-muted-foreground">{report.limitations.map((text) => <li key={text}>{text}</li>)}</ul>
      </div></details>
      <div className="space-y-2">{items.items.map((row) => <div key={row.id} className="flex flex-wrap items-center justify-between gap-2 rounded border p-3 text-sm"><span>{row.ordinal}. {row.name} · {row.reference_decision ?? "Unreviewed"}</span><Button variant="outline" disabled={busy} onClick={() => void run(() => loadItem(row.id))}>Review {row.name}</Button></div>)}</div>
      {items.next_cursor && <Button variant="outline" disabled={busy} onClick={() => void run(async () => { const page = await apiFetch<Page<ItemSummary>>(`${API}/${report.cohort.id}/items?after=${items.next_cursor}`); setItems({ items: [...items.items, ...page.items], next_cursor: page.next_cursor }); })}>More enrolled dossiers</Button>}
      {item && <article aria-label="Frozen qualification dossier" className="rounded border p-4 space-y-3">
        <div className="flex flex-wrap justify-between gap-2"><h4 className="font-medium">{item.snapshot.name} · dossier version {item.snapshot.version}</h4><Button variant="outline" disabled={busy} onClick={() => void run(() => loadItem(item.id))}>Reload review item</Button></div>
        <p className="text-sm">{item.snapshot.domain} · {item.buying_case.replaceAll("_", " ")} · policy {item.policy_version}</p>
        <p className="text-sm">{item.snapshot.dossier.summary}</p>
        {!!item.snapshot.dossier.unanswered.length && <p className="text-sm">Unknowns: {item.snapshot.dossier.unanswered.join("; ")}</p>}
        <div className="space-y-3">{item.snapshot.dossier.evidence.map((fact) => <div key={fact.id} className="border-l-2 pl-3 text-sm"><p>{fact.field}: {String(fact.value)} · {fact.confidence}</p><blockquote>{fact.excerpt}</blockquote><a className="underline" href={fact.url} target="_blank" rel="noopener noreferrer">Evidence source</a></div>)}</div>
        <details><summary className="cursor-pointer text-sm">Frozen policy rules</summary><LibraryEntry entry={{ kind: "qualification", version: item.policy_version, data: item.snapshot.policy }} statusLabel="Frozen policy snapshot" /></details>
        {!!item.assessments.length && <div className="space-y-2 text-sm"><p>Model decision: {item.snapshot.qualification.decision} · {item.snapshot.qualification.score}/100</p>
          {item.assessments.map((row) => <p key={row.id}>Assessment {row.revision}: {row.reference_decision} · {row.actor} · {row.reason}</p>)}
        </div>}
        <label className="block space-y-1 text-sm"><span>Your qualification assessment</span><select className="block w-full rounded border bg-background p-2" value={decision} disabled={busy || held} onChange={(event) => setDecision(event.target.value)}><option value="">Choose an assessment</option><option value="qualified">Qualified business fit</option><option value="rejected">Not a business fit</option><option value="needs_research">More research needed</option></select></label>
        <label className="block space-y-1 text-sm"><span>Assessment reason</span><textarea className="w-full rounded border bg-transparent p-2" maxLength={2000} value={reason} disabled={busy || held} onChange={(event) => setReason(event.target.value)} /></label>
        <Button disabled={busy || held || !decision || reason.trim().length < 10} onClick={() => void assess()}>{item.assessments.length ? "Record correction" : "Record assessment"}</Button>
      </article>}
    </>}
  </section>;
}
