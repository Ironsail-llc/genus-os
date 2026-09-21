"use client";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiFetch } from "@/lib/api/client";

type ContactData = { name: string; role: string; email: string; source_url: string; verification?: string; verified_at?: string | null };
type ContactItem = { id: string; data: ContactData; identity_hash: string; review: { current: boolean; actor?: string; at?: string } };
const blank = { name: "", role: "", email: "", source_url: "" };
export function ContactReview({ prospectId, onChanged }: { prospectId: string; onChanged: () => Promise<void> }) {
  const [contacts, setContacts] = useState<ContactItem[] | null>(null);
  const [editing, setEditing] = useState<ContactItem | "new" | null>(null);
  const [form, setForm] = useState<ContactData>(blank);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [held, setHeld] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const path = `/api/bridge/api/sales/prospects/${prospectId}/contacts`;
  async function load() {
    setBusy(true); setError(null);
    try { setContacts(await apiFetch<ContactItem[]>(path)); setEditing(null); setHeld(false); }
    catch (e) { setError(String(e)); setHeld(true); }
    finally { setBusy(false); }
  }
  function edit(item: ContactItem | "new") {
    setEditing(item); setForm(item === "new" ? blank : item.data); setReason(""); setNotice(null);
  }
  async function save() {
    if (!editing || held) return;
    setBusy(true); setError(null); setNotice(null);
    const data = { name: form.name.trim(), role: form.role.trim(), source_url: form.source_url.trim() };
    const body = editing === "new" ? { contact: { ...data, email: form.email.trim() }, reason: reason.trim() } : { ...data, expected_hash: editing.identity_hash, reason: reason.trim() };
    try {
      await apiFetch(path + (editing === "new" ? "" : `/${editing.id}/review`), { method: "POST", body: JSON.stringify(body) });
      setHeld(true); setNotice("Contact review recorded. Deliverability still comes from provider verification; old drafts need fresh review.");
      await onChanged(); await load();
    } catch (e) { setError(`${String(e)} Reload contacts before another decision.`); setHeld(true); }
    finally { setBusy(false); }
  }
  return <section className="space-y-3 rounded border p-3" aria-label="Business contact review">
    <Button variant="outline" disabled={busy} onClick={() => void load()}>{contacts ? "Reload business contacts" : "Review business contacts"}</Button>
    {contacts && <>
      <p className="text-sm text-muted-foreground">Review the public source separately from email deliverability. Correct shared inboxes and unknown roles before approving outreach. Identity changes cancel pending drafts; automated research cannot overwrite your reviewed identity.</p>
      {!contacts.length && <p>No contacts stored. Check contact-research job status in preparation recovery before rerunning, or add a public business contact below.</p>}
      {contacts.map((c) => <article key={c.id} className="space-y-2 rounded border p-3 text-sm">
        <p>{c.data.name} · {c.data.role} · {c.data.email}</p>
        <p>Deliverability: {c.data.verification ?? "unknown"}{c.data.verified_at ? ` · ${new Date(c.data.verified_at).toLocaleString()}` : ""}</p>
        <p>Identity: {c.review.current ? `reviewed by ${c.review.actor}` : "not yet reviewed"}</p>
        {/^https?:\/\//.test(c.data.source_url) && <a className="underline" href={c.data.source_url} target="_blank" rel="noopener noreferrer">Public contact source</a>}
        <Button variant="outline" disabled={busy || held} onClick={() => edit(c)}>Review this identity</Button>
      </article>)}
      <Button variant="outline" disabled={busy || held} onClick={() => edit("new")}>Add public business contact</Button>
    </>}
    {editing && <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <label className="block">Contact name<Input value={form.name} required maxLength={200} disabled={busy || held} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
      <label className="block">Business role<Input value={form.role} required maxLength={200} disabled={busy || held} onChange={(e) => setForm({ ...form, role: e.target.value })} /></label>
      <label className="block">Business email<Input type="email" value={form.email} required disabled={busy || held || editing !== "new"} onChange={(e) => setForm({ ...form, email: e.target.value })} /></label>
      <label className="block">Public business source URL<Input type="url" value={form.source_url} required disabled={busy || held} onChange={(e) => setForm({ ...form, source_url: e.target.value })} /></label>
      <label className="block">Contact review reason<textarea className="block w-full rounded border bg-background p-2" value={reason} required minLength={10} maxLength={2000} disabled={busy || held} onChange={(e) => setReason(e.target.value)} /></label>
      <Button type="submit" disabled={busy || held || reason.trim().length < 10}>{editing === "new" ? "Add reviewed contact for verification" : "Save reviewed identity"}</Button>
    </form>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </section>;
}
