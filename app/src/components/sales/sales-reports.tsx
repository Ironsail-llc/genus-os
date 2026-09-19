"use client";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { apiFetch } from "@/lib/api/client";

type Scalar = string | number | boolean | null;
type Row = { id: string; period: string; status: string; error?: string };
type Page = { items: Row[]; next_cursor: string | null };
type Report = { id: string; status: string; error?: string; prepared_at?: string; dataset?: { supporting_counts: Record<string, number | null>; population: { observed_businesses: number; known_businesses: number; complete: boolean }; costs: { actual_units: number; reserved_units: number }; cohorts: Record<string, Scalar>[]; qualification_reviews: { name: string; initial: Record<string, Scalar>; latest: Record<string, Scalar>; corrections: number; review_complete: boolean; agreement_target_met: boolean }[]; business_sources: Record<string, Scalar>[]; limitations: string[] }; analysis?: { cohort_summary: string; limitations: string[]; proposed_changes: { proposal: string; metric_paths: string[] }[] } };
const API = "/api/bridge/api/sales/reports";
const label = (value: string) => value.replaceAll("_", " ");
function Facts({ values }: { values: Record<string, Scalar> }) {
  return <dl className="space-y-1 text-sm">{Object.entries(values).map(([key, value]) => <div key={key} className="flex justify-between gap-4"><dt>{label(key)}</dt><dd>{value === null ? "Unknown" : String(value)}</dd></div>)}</dl>;
}
export function SalesReports() {
  const [page, setPage] = useState<Page | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selected = useRef(0);
  useEffect(() => { let active = true; apiFetch<Page>(API).then((data) => { if (active) setPage(data); }).catch((e) => { if (active) setError(String(e)); }); return () => { active = false; }; }, []);
  async function open(id: string) {
    const current = ++selected.current;
    setBusy(true); setError(null);
    try { const data = await apiFetch<Report>(`${API}/${id}`); if (current === selected.current) setReport(data); }
    catch (e) { if (current === selected.current) setError(String(e)); }
    finally { if (current === selected.current) setBusy(false); }
  }
  async function load(more = false) {
    setBusy(true); setError(null);
    try { const data = await apiFetch<Page>(API + (more && page?.next_cursor ? `?after=${encodeURIComponent(page.next_cursor)}` : "")); setPage((prior) => more && prior ? { ...data, items: [...prior.items, ...data.items] } : data); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  return <section className="space-y-3 rounded border p-4" aria-label="Sales intelligence reports">
    <h3 className="font-medium">Sales intelligence reports</h3>
    <p className="text-sm text-muted-foreground">The native analyst prepares one report per week while research is enabled. Counts come from recorded business activity. Recommendations remain proposals for human review.</p>
    <Button variant="outline" disabled={busy} onClick={() => void load()}>Refresh reports</Button>
    {error && <p role="alert">{error}</p>}
    {page && !page.items.length && <p>No reports yet. The analyst needs observed companies and an enabled research workflow.</p>}
    {page?.items.map((r) => <Button variant="outline" key={r.id} disabled={busy} onClick={() => void open(r.id)}>{r.period} · {r.status}</Button>)}
    {page?.next_cursor && <Button variant="outline" disabled={busy} onClick={() => void load(true)}>Load more reports</Button>}
    {report && <article className="space-y-4">
      <p>{report.status}{report.prepared_at ? ` · ${new Date(report.prepared_at).toLocaleString()}` : ""}</p>
      {report.error && <p>{report.error}</p>}
      {report.dataset && <>
        <p>Observed {report.dataset.population.observed_businesses} of {report.dataset.population.known_businesses} known businesses{report.dataset.population.complete ? "." : "; sample is incomplete."}</p>
        <Facts values={report.dataset.supporting_counts} />
        <p className="text-sm">Recorded sales costs: ${(report.dataset.costs.actual_units / 1e6).toFixed(4)} · Unsettled reservations: ${(report.dataset.costs.reserved_units / 1e6).toFixed(4)}</p>
        <h4 className="font-medium">Cohorts by buying case, policy and discovery month</h4>
        {report.dataset.cohorts.map((cohort, i) => <div className="rounded border p-3" key={i}><Facts values={cohort} /></div>)}
        <h4 className="font-medium">Qualification review</h4>
        {report.dataset.qualification_reviews.map((review, i) => <div key={i} className="rounded border p-3 space-y-2"><p>{review.name} · {review.review_complete ? "Review complete" : "Review incomplete"}</p><p>Original decisions</p><Facts values={review.initial} /><p>Latest decisions · {review.corrections} corrections</p><Facts values={review.latest} /></div>)}
        <h4 className="font-medium">Business source coverage</h4>
        {report.dataset.business_sources.map((source, i) => <Facts key={i} values={source} />)}
        {report.dataset.limitations.map((text) => <p className="text-sm text-muted-foreground" key={text}>{text}</p>)}
      </>}
      {report.analysis && <>
        <h4 className="font-medium">Analyst interpretation</h4><p>{report.analysis.cohort_summary}</p>
        {report.analysis.limitations.map((text) => <p className="text-sm" key={text}>{text}</p>)}
        <h4 className="font-medium">Proposed changes</h4>
        {report.analysis.proposed_changes.map((proposal, i) => <div className="rounded border p-3" key={i}><p>{proposal.proposal}</p><p className="text-xs text-muted-foreground">Supporting metrics: {proposal.metric_paths.map(label).join(", ")}</p></div>)}
      </>}
    </article>}
  </section>;
}
