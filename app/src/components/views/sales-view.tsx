"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/business/page-header";
import { isOperatorRole } from "@/components/layout/nav-config";
import { apiFetch } from "@/lib/api/client";
import { BusinessReview, type BusinessSource } from "@/components/sales/business-review";
import { ReadRecovery } from "@/components/sales/read-recovery";
import { DeploymentControls } from "@/components/sales/deployment-controls";
import { PilotSettings } from "@/components/sales/pilot-settings";
import { SalesLibrary } from "@/components/sales/sales-library";
import { CalibrationReview } from "@/components/sales/calibration-review";
import { QualificationAssessment, type Assessment } from "@/components/sales/qualification-assessment";
import { PipedriveIdentity } from "@/components/sales/pipedrive-identity";
import { ProspectRecovery } from "@/components/sales/prospect-recovery";
import { ResearchRequests } from "@/components/sales/research-requests";

const API = "/api/bridge/api/sales";
type Evidence = { id: string; field: string; value: unknown; url: string; excerpt: string; retrieved_at: string; confidence: string };
type Prospect = { id: string; version: number; name: string; domain: string; status: string; owner: string;
  qualification?: { policy_version?: string; score: number; decision: string; missing: string[]; buying_case: string; assessment?: Assessment };
  dossier?: { summary: string; unanswered: string[]; evidence: Evidence[] } };
type Action = { id: string; status: string; expires_at: string; receipt?: { id?: string; delivery_status?: string }; payload: {
  prospect_id: string; sender: string; recipient: string; subject: string; body: string;
  claim_ids: string[]; evidence_ids: string[]; knowledge_version: string; purpose: string } };
type Overview = { prospects: Prospect[]; actions: Action[]; jobs: { id: string; kind: string; status: string; error?: string }[];
  budgets: { scope: string; spent_units: number; reserved_units: number; limit_units: number }[];
  settings: Record<string, unknown> };
type Detail = { prospect: Prospect; contacts: { id: string; email: string; data: { name: string; role: string; verification: string } }[];
  messages: { provider_id: string; direction: string; occurred_at: string; data: { subject: string; body: string } }[];
  retention: Record<string, string | number | boolean | null> };
const switches = [ ["research_enabled", "Research"], ["enrichment_enabled", "Contact enrichment"],
  ["promotion_enabled", "Pipedrive sync"], ["sending_enabled", "Approved email delivery"], ["outcomes_enabled", "Customer outcomes"] ];

export function SalesView({ visible, role }: { visible: boolean; role?: string | null }) {
  const [data, setData] = useState<Overview | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showBusiness, setShowBusiness] = useState(false);
  const [showReads, setShowReads] = useState(false);
  const [showDeployment, setShowDeployment] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showLibrary, setShowLibrary] = useState(false);
  const [showCalibration, setShowCalibration] = useState(false);
  const [showRequests, setShowRequests] = useState(false);
  const permitted = isOperatorRole(role);
  const refresh = useCallback(async () => {
    const latest = await apiFetch<Overview>(API);
    setData(latest);
  }, []);
  useEffect(() => {
    if (!visible || !permitted) return;
    let current = true;
    apiFetch<Overview>(API).then((result) => { if (current) { setData(result); setError(null); } })
      .catch((e) => { if (current) setError(String(e)); });
    return () => { current = false; };
  }, [visible, permitted]);
  async function mutate(path: string, body: unknown, method = "POST") {
    setBusy(true); setError(null);
    try {
      await apiFetch(API + path, { method, body: JSON.stringify(body) });
      await refresh();
      if (detail) setDetail(await apiFetch<Detail>(`${API}/prospects/${detail.prospect.id}`));
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  async function openProspect(id: string) {
    setError(null);
    try { setDetail(await apiFetch<Detail>(`${API}/prospects/${id}`)); }
    catch (e) { setError(String(e)); }
  }
  if (!visible) return null;
  if (!permitted) return <div className="p-6">Sales operator access required.</div>;
  return <section className="h-full overflow-auto p-5 space-y-5" aria-label="Sales intelligence">
    <PageHeader title="Sales" description="Evidence, review, conversations, and customer outcomes">
      <Button variant="outline" disabled={busy} onClick={() => void refresh().catch((e) => setError(String(e)))}>Refresh</Button>
    </PageHeader>
    {error && <p role="alert" className="rounded border border-destructive p-3 text-destructive">{error}</p>}
    {!data ? <p>Loading sales workspace…</p> : <>
      <div className="flex flex-wrap gap-2" aria-label="Sales controls">{switches.map(([key, label]) =>
        <Button key={key} variant="outline" disabled={busy} aria-pressed={data.settings[key] === true}
          onClick={() => void mutate("/settings", { [key]: data.settings[key] !== true }, "PATCH")}>
          {label}: {data.settings[key] === true ? "enabled" : "paused"}
        </Button>)}</div>
      <p className="text-sm text-muted-foreground">Every outbound message requires individual approval. Provider acceptance and confirmed delivery are tracked separately.</p>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" aria-expanded={showRequests} onClick={() => setShowRequests(!showRequests)}>Research requests</Button>
        <Button variant="outline" aria-expanded={showBusiness} onClick={() => setShowBusiness(!showBusiness)}>Review imported practices</Button>
        <Button variant="outline" aria-expanded={showReads} onClick={() => setShowReads(!showReads)}>Inspect provider reads</Button>
        <Button variant="outline" aria-expanded={showDeployment} onClick={() => setShowDeployment(!showDeployment)}>Manage sales deployment</Button>
        <Button variant="outline" aria-expanded={showSettings} onClick={() => setShowSettings(!showSettings)}>Review pilot settings</Button>
        <Button variant="outline" aria-expanded={showLibrary} onClick={() => setShowLibrary(!showLibrary)}>Review sales library</Button>
        <Button variant="outline" aria-expanded={showCalibration} onClick={() => setShowCalibration(!showCalibration)}>Assess qualification quality</Button>
      </div>
      {showBusiness && <BusinessReview prospects={data.prospects} sources={(data.settings.business_sources as BusinessSource[] | undefined) ?? []}
        onChanged={async () => { await refresh(); if (detail) setDetail(await apiFetch<Detail>(`${API}/prospects/${detail.prospect.id}`)); }} />}
      {showReads && <ReadRecovery />}
      {showDeployment && <DeploymentControls onChanged={refresh} />}
      {showSettings && <PilotSettings onChanged={refresh} />}
      {showLibrary && <SalesLibrary onChanged={refresh} />}
      {showCalibration && <CalibrationReview />}
      {showRequests && <ResearchRequests buyingCases={Object.keys((data.settings.active_policy_versions as Record<string, string> | undefined) ?? {})} />}
      <div className="grid gap-5 xl:grid-cols-2">
        <div className="space-y-3">
          <h3 className="font-medium">Prospects ({data.prospects.length} shown)</h3>
          {!data.prospects.length && <p className="text-sm text-muted-foreground">No prospects yet. Enable the configured discovery workflow to begin.</p>}
          {data.prospects.map((p) => <button key={p.id} onClick={() => void openProspect(p.id)}
            className="w-full rounded-lg border p-3 text-left hover:bg-muted/40">
            <span className="font-medium">{p.name}</span><span className="float-right text-sm">{p.qualification?.score ?? "Unscored"} · {p.status}</span>
            <p className="text-sm text-muted-foreground">{p.domain} · {p.qualification?.buying_case ?? "Research pending"}</p>
          </button>)}
          {detail && <article className="space-y-3 rounded-lg border p-4" aria-label="Prospect dossier">
            <h3 className="font-medium">{detail.prospect.name}</h3>
            <p>{detail.prospect.dossier?.summary}</p>
            <p className="text-sm">Owner: {detail.prospect.owner}</p>
            <div className="flex gap-2 flex-wrap">
              <Button disabled={busy || detail.prospect.qualification?.decision !== "qualified"} onClick={() => void mutate(`/prospects/${detail.prospect.id}/review`, { approved: true, expected_version: detail.prospect.version, expected_policy_version: detail.prospect.qualification?.policy_version ?? null })}>Accept prospect</Button>
              <Button variant="outline" disabled={busy} onClick={() => void mutate(`/prospects/${detail.prospect.id}/review`, { approved: false, expected_version: detail.prospect.version, expected_policy_version: detail.prospect.qualification?.policy_version ?? null })}>Reject prospect</Button>
              <Button variant="outline" disabled={busy} onClick={() => void mutate(`/prospects/${detail.prospect.id}/takeover`, {})}>Take over conversation</Button>
            </div>
            <PipedriveIdentity key={`pipedrive-${detail.prospect.id}`} prospectId={detail.prospect.id} onChanged={async () => { await refresh(); setDetail(await apiFetch<Detail>(`${API}/prospects/${detail.prospect.id}`)); }} />
            <ProspectRecovery key={detail.prospect.id} prospectId={detail.prospect.id} onChanged={async () => { await refresh(); setDetail(await apiFetch<Detail>(`${API}/prospects/${detail.prospect.id}`)); }} />
            <QualificationAssessment assessment={detail.prospect.qualification?.assessment} evidence={detail.prospect.dossier?.evidence ?? []} />
            <h4 className="font-medium">Original research evidence</h4>
            {detail.prospect.dossier?.evidence.map((e) => <div key={e.id} className="border-l-2 pl-3 text-sm">
              <p>{e.field}: {String(e.value)} · {e.confidence}</p>
              <blockquote className="text-muted-foreground">{e.excerpt}</blockquote>
              <a href={e.url} target="_blank" rel="noopener noreferrer" className="underline">Source · {new Date(e.retrieved_at).toLocaleDateString()}</a>
            </div>)}
            {!!detail.prospect.dossier?.unanswered.length && <p className="text-sm">Unknown: {detail.prospect.dossier.unanswered.join("; ")}</p>}
            <h4 className="font-medium">Contacts</h4>
            {detail.contacts.map((c) => <div key={c.id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span>{c.data.name} · {c.data.role} · {c.email} · {c.data.verification}</span>
              <Button variant="outline" disabled={busy} onClick={() => void mutate("/suppression", { email: c.email, reason: "Operator suppression" })}>Stop outreach</Button>
            </div>)}
            <h4 className="font-medium">Conversation</h4>
            {detail.messages.map((m) => <div key={m.provider_id} className="rounded bg-muted/30 p-3 text-sm">
              <p>{m.direction} · {new Date(m.occurred_at).toLocaleString()} · {m.data.subject}</p>
              <p className="whitespace-pre-wrap">{m.data.body}</p>
            </div>)}
            <h4 className="font-medium">Activation and retention</h4>
            <dl className="space-y-1 text-sm">{Object.entries(detail.retention).map(([key, value]) => <div key={key} className="flex justify-between gap-3">
              <dt>{key.replaceAll("_", " ")}</dt><dd>{value === null ? "Not yet measurable" : String(value)}</dd>
            </div>)}</dl>
          </article>}
        </div>
        <div className="space-y-3">
          <h3 className="font-medium">Message review</h3>
          {data.actions.filter((a) => a.status === "review").map((a) => <article key={a.id} className="rounded-lg border p-4 space-y-3">
            <p className="text-sm">{a.payload.sender} → {a.payload.recipient} · {a.payload.purpose}</p>
            <h4 className="font-medium">{a.payload.subject}</h4>
            <p className="whitespace-pre-wrap text-sm">{a.payload.body}</p>
            <p className="text-xs text-muted-foreground">Claims: {a.payload.claim_ids.join(", ")} · Evidence: {a.payload.evidence_ids.join(", ")}</p>
            <Button variant="link" onClick={() => void openProspect(a.payload.prospect_id)}>View supporting dossier</Button>
            <div className="flex gap-2">
              <Button disabled={busy} onClick={() => void mutate(`/actions/${a.id}/decision`, { approved: true })}>Approve this message</Button>
              <Button variant="outline" disabled={busy} onClick={() => void mutate(`/actions/${a.id}/decision`, { approved: false })}>Reject</Button>
            </div>
          </article>)}
          {!data.actions.some((a) => a.status === "review") && <p className="text-sm text-muted-foreground">No messages awaiting review.</p>}
          <h3 className="font-medium">Delivery and sync</h3>
          {data.actions.filter((a) => a.status !== "review").map((a) => <p key={a.id} className="text-sm">{a.payload.recipient}: {a.receipt?.delivery_status ?? a.status}</p>)}
          {data.jobs.filter((j) => j.error || j.status === "failed").map((j) => <p key={j.id} className="text-sm">{j.kind}: {j.status} · {j.error}</p>)}
          <h3 className="font-medium">Spending</h3>
          {data.budgets.map((b) => <p key={b.scope} className="text-sm">{b.scope}: ${(b.spent_units / 1e6).toFixed(2)} spent · ${(b.reserved_units / 1e6).toFixed(2)} reserved · ${(b.limit_units / 1e6).toFixed(2)} limit</p>)}
        </div>
      </div>
    </>}
  </section>;
}
