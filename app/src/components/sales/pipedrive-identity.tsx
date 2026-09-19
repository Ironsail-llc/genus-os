"use client";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiFetch } from "@/lib/api/client";

type RecordRef = { id: number | string; name?: string; title?: string; email?: string; organization_id?: number };
type Packet = { id: string; content_hash: string; content: { observed_at: string; records: { organization: RecordRef; people: RecordRef[]; lead: RecordRef | null } } };
type Matches = { organizations: RecordRef[]; people: (RecordRef & { searched_email: string })[] };
export function PipedriveIdentity({ prospectId, onChanged }: { prospectId: string; onChanged: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [organization, setOrganization] = useState("");
  const [people, setPeople] = useState("");
  const [lead, setLead] = useState("");
  const [matches, setMatches] = useState<Matches | null>(null);
  const [packet, setPacket] = useState<Packet | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [held, setHeld] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const path = `/api/bridge/api/sales/prospects/${prospectId}/pipedrive`;
  async function search() {
    setBusy(true); setError(null);
    try { setMatches(await apiFetch<Matches>(path + "/matches")); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  async function inspect() {
    setError(null); setNotice(null); setPacket(null); setHeld(true);
    const personIds = people.trim() ? people.split(",").map((v) => Number(v.trim())) : [];
    if (!/^[1-9]\d*$/.test(organization) || personIds.length > 5 || personIds.some((n) => !Number.isSafeInteger(n) || n <= 0)) { setError("Enter a positive organization ID and up to five comma-separated person IDs."); return; }
    setBusy(true);
    try { setPacket(await apiFetch<Packet>(path + "/inspect", { method: "POST", body: JSON.stringify({ organization_id: Number(organization), person_ids: personIds, lead_id: lead.trim() || null }) })); setHeld(false); setReason(""); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  async function adopt() {
    if (!packet || held || reason.trim().length < 10) return;
    setBusy(true); setError(null); setHeld(true);
    try { await apiFetch(path + "/adopt", { method: "POST", body: JSON.stringify({ packet_id: packet.id, expected_hash: packet.content_hash, reason: reason.trim() }) }); setNotice("Identity adoption recorded in Genus. Promotion still follows its existing switch and review controls."); await onChanged(); }
    catch (e) { setError(`${String(e)} Fetch again before another decision.`); }
    finally { setBusy(false); }
  }
  return <section className="space-y-3 rounded border p-3" aria-label="Pipedrive identity review">
    <Button variant="outline" aria-expanded={open} onClick={() => setOpen(!open)}>Review existing Pipedrive identities</Button>
    {open && <>
      <p className="text-sm text-muted-foreground">Search and fetch read Pipedrive using the configured vault account. Review the organization, contact email and relationships before adopting. Adoption records existing IDs in Genus; it does not create provider records or approve email.</p>
      <Button variant="outline" disabled={busy} onClick={() => void search()}>Search Pipedrive matches</Button>
      {matches && <div className="text-sm">{matches.organizations.map((r) => <p key={`org-${r.id}`}>Organization {r.id}: {r.name}</p>)}{matches.people.map((r, i) => <p key={`person-${i}`}>Person {r.id}: {r.name} · searched {r.searched_email}</p>)}{!matches.organizations.length && !matches.people.length && <p>No matches in the bounded search. You can enter IDs from Pipedrive.</p>}</div>}
      <label className="block">Organization ID<Input value={organization} disabled={busy} onChange={(e) => { setOrganization(e.target.value); setPacket(null); }} /></label>
      <label className="block">Person IDs (optional)<Input value={people} disabled={busy} onChange={(e) => { setPeople(e.target.value); setPacket(null); }} /></label>
      <label className="block">Existing lead ID (optional)<Input value={lead} disabled={busy} onChange={(e) => { setLead(e.target.value); setPacket(null); }} /></label>
      <Button variant="outline" disabled={busy} onClick={() => void inspect()}>Fetch selected records</Button>
      {packet && <div className="space-y-2 text-sm">
        <p>Organization {packet.content.records.organization.id}: <span>{packet.content.records.organization.name}</span></p>
        {packet.content.records.people.map((p) => <p key={p.id}>Person {p.id}: {p.name} · {p.email} · organization {p.organization_id}</p>)}
        {packet.content.records.lead && <p>Lead {packet.content.records.lead.id}: {packet.content.records.lead.title}</p>}
        <p>Fetched {new Date(packet.content.observed_at).toLocaleString()}; review expires after 15 minutes.</p>
        <label className="block">Identity review reason<textarea className="block w-full rounded border bg-background p-2" value={reason} disabled={busy || held} maxLength={2000} onChange={(e) => setReason(e.target.value)} /></label>
        <Button disabled={busy || held || reason.trim().length < 10} onClick={() => void adopt()}>Adopt reviewed identities</Button>
      </div>}
    </>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </section>;
}
