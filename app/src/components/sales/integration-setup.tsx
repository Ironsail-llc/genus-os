"use client";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiFetch } from "@/lib/api/client";

type Row = Record<string, string | number>;
type Config = { email_provider?: "none" | "gmail" | "instantly"; senders: string[]; mailbox_approved_until: Record<string, string>; postal_address: string; unsubscribe_url: string; business_sources: Row[]; discovery_segments: Row[] };
type Snapshot = { revision: number; config: Partial<Config>; credentials: { provider: string; complete: boolean; keys: { name: string; present: boolean }[] }[]; business_services: { source: string; installed: boolean }[]; provider_checks: { id: string; status: string; error?: string; result?: { scope?: string; status?: string; sender?: string; authenticated?: boolean }; updated_at: string }[]; notes: string[] };
const defaults: Config = { senders: [], mailbox_approved_until: {}, postal_address: "", unsubscribe_url: "", business_sources: [], discovery_segments: [] };
const API = "/api/bridge/api/sales/setup";
const labels: Record<string, string> = { senders: "Sending mailboxes", mailbox_approved_until: "Mailbox readiness review", postal_address: "Business postal address", unsubscribe_url: "Opt-out link", business_sources: "Business sources", discovery_segments: "Scheduled discovery segments" };
const rowFields = {
  mailbox: [["email", "Mailbox email", "email"], ["until", "Readiness expiry (include timezone)", "text"]],
  source: [["source", "Provider identifier", "text"], ["account_id", "Business account ID", "text"], ["refresh_seconds", "Refresh interval seconds", "number"]],
  segment: [["id", "Segment ID", "text"], ["buying_case", "Buying case", "text"], ["query", "Business search brief", "text"]],
};
function Rows({ kind, rows, onChange, disabled }: { kind: keyof typeof rowFields; rows: Row[]; onChange: (rows: Row[]) => void; disabled: boolean }) {
  return <>{rows.map((row, index) => <div className="space-y-2 rounded border p-3" key={index}>{rowFields[kind].map(([key, label, type]) => <label key={key} className="block">{label} {index + 1}<Input type={type} value={row[key] ?? ""} disabled={disabled} onChange={(e) => onChange(rows.map((r, i) => i === index ? { ...r, [key]: type === "number" ? Number(e.target.value) : e.target.value } : r))} /></label>)}<Button variant="outline" disabled={disabled} onClick={() => onChange(rows.filter((_, i) => i !== index))}>Remove {kind} {index + 1}</Button></div>)}<Button variant="outline" disabled={disabled || rows.length >= (kind === "mailbox" ? 100 : kind === "source" ? 10 : 50)} onClick={() => onChange([...rows, Object.fromEntries(rowFields[kind].map(([key, , type]) => [key, type === "number" ? 21600 : ""]))])}>Add {kind}</Button></>;
}
function display(value: unknown): string {
  if (Array.isArray(value)) return value.map(display).join("; ") || "None";
  if (value && typeof value === "object") return Object.entries(value).map(([key, entry]) => `${key.replaceAll("_", " ")}: ${display(entry)}`).join(" · ") || "None";
  return String(value || "Not configured");
}
export function IntegrationSetup({ onChanged }: { onChanged: () => Promise<void> }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [mailboxes, setMailboxes] = useState<Row[]>([]);
  const [sources, setSources] = useState<Row[]>([]);
  const [segments, setSegments] = useState<Row[]>([]);
  const [address, setAddress] = useState("");
  const [unsubscribe, setUnsubscribe] = useState("");
  const [reason, setReason] = useState("");
  const [changes, setChanges] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const [held, setHeld] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  function accept(value: Snapshot) {
    const config = { ...defaults, ...value.config };
    setSnapshot(value); setMailboxes(config.senders.map((email) => ({ email, until: config.mailbox_approved_until[email] ?? "" }))); setSources(config.business_sources); setSegments(config.discovery_segments); setAddress(config.postal_address); setUnsubscribe(config.unsubscribe_url); setReason(""); setChanges(null); setHeld(false);
  }
  useEffect(() => { let active = true; apiFetch<Snapshot>(API).then((data) => { if (active) accept(data); }).catch((e) => { if (active) setError(String(e)); }); return () => { active = false; }; }, []);
  async function reload() { setBusy(true); setError(null); try { accept(await apiFetch<Snapshot>(API)); } catch (e) { setError(String(e)); setHeld(true); } finally { setBusy(false); } }
  function review() {
    if (!snapshot) return;
    setError(null); setNotice(null);
    if (reason.trim().length < 10) { setError("Explain the setup change in at least 10 characters."); return; }
    const emails = mailboxes.map((s) => String(s.email).trim().toLowerCase());
    if (emails.some((email) => !email.includes("@")) || new Set(emails).size !== emails.length) { setError("Use a distinct business email for each mailbox."); return; }
    const expiry: Record<string, string> = {};
    for (const [index, row] of mailboxes.entries()) {
      const until = String(row.until).trim();
      if (until && (!/(Z|[+-]\d{2}:\d{2})$/.test(until) || Number.isNaN(Date.parse(until)))) { setError("Readiness expiry needs a valid date and timezone, for example 2026-10-01T17:00:00Z. Leave blank if unreviewed."); return; }
      if (until) expiry[emails[index]] = until;
    }
    if (sources.some((s) => !String(s.source).trim() || !String(s.account_id).trim() || !Number.isInteger(s.refresh_seconds) || Number(s.refresh_seconds) < 600)) { setError("Each business source needs a provider identifier, account ID and refresh interval of at least 600 seconds."); return; }
    if (segments.some((s) => !String(s.id).trim() || !String(s.buying_case).trim() || !String(s.query).trim())) { setError("Complete each segment ID, buying case and business search brief."); return; }
    const next: Config = { senders: emails, mailbox_approved_until: expiry, postal_address: address.trim(), unsubscribe_url: unsubscribe.trim(), business_sources: sources, discovery_segments: segments };
    const old = { ...defaults, ...snapshot.config };
    const diff = Object.fromEntries(Object.entries(next).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(old[key as keyof Config])));
    if (!Object.keys(diff).length) { setError("No setup changes to review."); return; }
    setChanges(diff);
  }
  async function save() {
    if (!snapshot || !changes || held) return;
    setBusy(true); setError(null); setHeld(true);
    try { accept(await apiFetch<Snapshot>(API, { method: "POST", body: JSON.stringify({ expected_revision: snapshot.revision, reason: reason.trim(), changes }) })); setNotice("Setup saved. Integration switches and message approvals were not enabled."); await onChanged(); }
    catch (e) { setError(`${String(e)} Reload before another setup decision.`); }
    finally { setBusy(false); }
  }
  const disabled = busy || held || !!changes;
  return <section className="space-y-4 rounded border p-4" aria-label="Sales integration setup">
    <h3 className="font-medium">Sales integration setup</h3>
    <p className="text-sm text-muted-foreground">Keep API credentials in the Genus vault. This view shows required key names; their presence does not prove connectivity or a successful live pilot.</p>
    <Button variant="outline" disabled={busy} onClick={() => void reload()}>Reload setup</Button>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {snapshot && <>
      {snapshot.credentials.map((provider) => <div key={provider.provider} className="text-sm"><p className="font-medium">{provider.provider}: {provider.complete ? "Required keys present" : "Keys missing"}</p>{provider.keys.map((key) => <p key={key.name}><span>{key.name}</span> · {key.present ? "present" : "missing"}</p>)}</div>)}
      {snapshot.business_services.map((source) => <p key={source.source}>{source.source} adapter: {source.installed ? "installed" : "not installed"}</p>)}
      <label className="block">Business postal address<Input value={address} maxLength={1000} disabled={disabled} onChange={(e) => setAddress(e.target.value)} /></label>
      <label className="block">Opt-out URL<Input type="url" value={unsubscribe} disabled={disabled} onChange={(e) => setUnsubscribe(e.target.value)} /></label>
      <h4 className="font-medium">Sending mailboxes</h4><p className="text-sm text-muted-foreground">Leave readiness expiry blank until reviewed. {snapshot.config.email_provider === "gmail" ? "Delivery also checks the connected Gmail account and sending limits. Account access does not replace your mailbox readiness review." : "Delivery also checks live account status, warmup history and health."}</p>
      <Rows kind="mailbox" rows={mailboxes} onChange={setMailboxes} disabled={disabled} />
      <h4 className="font-medium">Business sources</h4><Rows kind="source" rows={sources} onChange={setSources} disabled={disabled} />
      <h4 className="font-medium">Scheduled discovery segments</h4><p className="text-sm text-muted-foreground">Used only when scheduled discovery is selected in pilot settings. Bounded requests have their own briefs.</p><Rows kind="segment" rows={segments} onChange={setSegments} disabled={disabled} />
      <label className="block">Setup review reason<textarea className="block w-full rounded border bg-background p-2" value={reason} maxLength={2000} disabled={disabled} onChange={(e) => setReason(e.target.value)} /></label>
      {!changes ? <Button disabled={busy || held} onClick={review}>Review setup changes</Button> : <div className="space-y-3 rounded border p-3">{Object.entries(changes).map(([key, value]) => <div key={key}><p className="font-medium">{labels[key]}</p><p>Before: {display(({ ...defaults, ...snapshot.config })[key as keyof Config])}</p><p>After: {display(value)}</p></div>)}<Button disabled={busy || held} onClick={() => void save()}>Save reviewed setup</Button><Button variant="outline" disabled={busy || held} onClick={() => setChanges(null)}>Edit setup changes</Button></div>}
      <h4 className="font-medium">Recent provider status checks</h4>{!snapshot.provider_checks.length && <p>No provider status checks recorded.</p>}{snapshot.provider_checks.map((check) => <p className="text-sm" key={check.id}>{check.result?.sender ?? check.result?.scope ?? "Provider read"}: {check.result?.authenticated === true ? "Account authenticated; readiness requires review" : check.result?.status ?? check.status} · {check.error} · {new Date(check.updated_at).toLocaleString()}</p>)}
      {snapshot.notes.map((note) => <p key={note} className="text-sm text-muted-foreground">{note}</p>)}
    </>}
  </section>;
}
