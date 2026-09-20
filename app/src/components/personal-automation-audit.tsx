"use client";

import { useEffect, useRef, useState } from "react";

type Entry = { id: string; version: number; grant_version: number; phase: string; created_at: string; redacted_at?: string | null };
type Snapshot = { phase?: string; coverage?: "visible_text_only" | "visible_text_and_selected_documents" | "suppressed_after_code"; capture_status?: "captured" | "withheld_after_code" | "unavailable"; omitted_frames: number; documents: { origin: string; text: string; links: string[]; source?: string; source_url?: string; requested_url?: string; text_truncated: boolean; links_truncated: boolean }[] };

export function PersonalAutomationAudit({ operationId, includeReceipts = false }: { operationId: string; includeReceipts?: boolean }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<Entry[]>([]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [redactedAt, setRedactedAt] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  async function load(id?: string) {
    pending.current?.abort();
    const request = new AbortController(); pending.current = request;
    setOpen(true); setBusy(true); setSnapshot(null); setRedactedAt(null); setMessage("");
    try {
      const response = await fetch(`/api/bridge/api/autonomy/operations/${operationId}/terms${id ? `/${id}` : ""}`, {cache: "no-store", signal: request.signal});
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      if (request.signal.aborted) return;
      if (id) { setSnapshot(data.snapshot); setRedactedAt(data.redacted_at ?? null); }
      else { setRows(data.snapshots); if (!data.snapshots.length) setMessage("No submission record was captured for this task."); }
    } catch {
      if (!request.signal.aborted) setMessage("Submission record unavailable. Please try again.");
    } finally {
      if (!request.signal.aborted) setBusy(false);
    }
  }
  // The owner's own delete. The archive held their name, date of birth,
  // address and the answers they typed into a website, and nothing but the
  // retention window ever removed it. What survives the call is the audit
  // fact — that a snapshot was taken, when, and under which grant version —
  // so the listing is reloaded rather than emptied.
  async function erase() {
    pending.current?.abort();
    const request = new AbortController(); pending.current = request;
    setBusy(true); setSnapshot(null); setRedactedAt(null); setMessage("");
    try {
      const response = await fetch(`/api/bridge/api/autonomy/operations/${operationId}/terms`, {method: "DELETE", cache: "no-store", signal: request.signal});
      if (!response.ok) throw new Error("unavailable");
      await response.json();
      if (request.signal.aborted) return;
      setMessage("Submission record erased. The record of when it was taken remains.");
      const listing = await fetch(`/api/bridge/api/autonomy/operations/${operationId}/terms`, {cache: "no-store", signal: request.signal});
      if (listing.ok && !request.signal.aborted) setRows((await listing.json()).snapshots);
    } catch {
      if (!request.signal.aborted) setMessage("Submission record unavailable. Please try again.");
    } finally {
      if (!request.signal.aborted) setBusy(false);
    }
  }
  function close() {
    pending.current?.abort(); setOpen(false); setRows([]); setSnapshot(null); setRedactedAt(null); setMessage(""); setBusy(false);
  }
  const capturedUrls = new Set(snapshot?.documents.flatMap(document => [document.source_url, document.requested_url]).filter(Boolean));
  if (!open) return <button className="text-sm underline" onClick={() => void load()}>{includeReceipts ? "Submission and receipts" : "Submission record"}</button>;
  return <div className="space-y-3 rounded border p-3">
    <button className="text-sm underline" onClick={close}>{includeReceipts ? "Close submission and receipts" : "Close submission record"}</button>
    <p className="text-sm text-muted-foreground">These are private observations from the browser. They do not prove acceptance or payment. Page text capture stops after a verification code is entered.</p>
    {busy && <p role="status">Loading submission record…</p>}
    {message && <p role="status">{message}</p>}
    {rows.map(row => <div key={row.id} className="text-sm">
      <button disabled={busy} className="underline" onClick={() => void load(row.id)}>View snapshot {row.version}</button>
      <span> · {row.phase === "after_confirmation" ? "Receipt after confirmation" : row.phase === "before_input" ? "Before filling" : "Before submission"} · Grant version {row.grant_version} · {row.created_at}{row.redacted_at ? ` · erased ${row.redacted_at}` : ""}</span>
    </div>)}
    {rows.length > 0 && rows.some(row => !row.redacted_at) && <button disabled={busy} className="text-sm underline" onClick={() => void erase()}>Erase submission record</button>}
    {redactedAt && <p>This record was erased on {redactedAt}. Only the fact that it was taken remains.</p>}
    {snapshot && <div className="space-y-3">
      {snapshot.phase === "after_confirmation" && snapshot.capture_status === "captured" && <p>Receipt record contains rendered page text only.</p>}
      {snapshot.capture_status === "withheld_after_code" && <p>Receipt text was not saved because a verification code was used.</p>}
      {snapshot.capture_status === "unavailable" && <p>Receipt text could not be captured. The recorded task outcome is unchanged.</p>}
      {snapshot.coverage === "suppressed_after_code" && <p>Page text was not saved for this step because a verification code was entered first. This record exists so the omission is visible.</p>}
      {snapshot.omitted_frames > 0 && <p className="text-sm">Some embedded pages were not captured.</p>}
      {snapshot.coverage !== "suppressed_after_code" && (!snapshot.capture_status || snapshot.capture_status === "captured") && snapshot.documents.map((document, index) => {
        const uncapturedLinks = document.links.filter(link => !capturedUrls.has(link));
        return <section key={index} className="space-y-2">
        <p className="font-medium">{document.origin}</p>
        {document.source === "linked_document" && <p className="break-all text-sm">Captured linked document · {document.source_url}</p>}
        {document.text_truncated && <p className="text-sm">Text was shortened to fit the record limit.</p>}
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-3 text-sm">{document.text}</pre>
        {uncapturedLinks.length > 0 && <><p className="text-sm">Contents of these links were not captured:</p>
          <ul className="space-y-1 text-xs">{uncapturedLinks.map((link, i) => <li key={i} className="break-all">{link}</li>)}</ul></>}
        {document.links_truncated && <p className="text-sm">Some link references were omitted.</p>}
      </section>; })}
    </div>}
  </div>;
}
