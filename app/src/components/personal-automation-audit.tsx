"use client";

import { useEffect, useRef, useState } from "react";

type Entry = { id: string; version: number; grant_version: number; phase: string; created_at: string };
type Snapshot = { omitted_frames: number; documents: { origin: string; text: string; links: string[]; text_truncated: boolean; links_truncated: boolean }[] };

export function PersonalAutomationAudit({ operationId }: { operationId: string }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<Entry[]>([]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  async function load(id?: string) {
    pending.current?.abort();
    const request = new AbortController(); pending.current = request;
    setOpen(true); setBusy(true); setSnapshot(null); setMessage("");
    try {
      const response = await fetch(`/api/bridge/api/autonomy/operations/${operationId}/terms${id ? `/${id}` : ""}`, {cache: "no-store", signal: request.signal});
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      if (request.signal.aborted) return;
      if (id) setSnapshot(data.snapshot);
      else { setRows(data.snapshots); if (!data.snapshots.length) setMessage("No submission record was captured for this task."); }
    } catch {
      if (!request.signal.aborted) setMessage("Submission record unavailable. Please try again.");
    } finally {
      if (!request.signal.aborted) setBusy(false);
    }
  }
  function close() {
    pending.current?.abort(); setOpen(false); setRows([]); setSnapshot(null); setMessage(""); setBusy(false);
  }
  if (!open) return <button className="text-sm underline" onClick={() => void load()}>Submission record</button>;
  return <div className="space-y-3 rounded border p-3">
    <button className="text-sm underline" onClick={close}>Close submission record</button>
    <p className="text-sm text-muted-foreground">These are private observations from the browser. They do not prove acceptance or payment. Capture stops after a verification code is entered.</p>
    {busy && <p role="status">Loading submission record…</p>}
    {message && <p role="status">{message}</p>}
    {rows.map(row => <div key={row.id} className="text-sm">
      <button disabled={busy} className="underline" onClick={() => void load(row.id)}>View snapshot {row.version}</button>
      <span> · {row.phase === "before_input" ? "Before filling" : "Before submission"} · Grant version {row.grant_version} · {row.created_at}</span>
    </div>)}
    {snapshot && <div className="space-y-3">
      {snapshot.omitted_frames > 0 && <p className="text-sm">Some embedded pages were not captured.</p>}
      {snapshot.documents.map((document, index) => <section key={index} className="space-y-2">
        <p className="font-medium">{document.origin}</p>
        {document.text_truncated && <p className="text-sm">Text was shortened to fit the record limit.</p>}
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-3 text-sm">{document.text}</pre>
        {document.links.length > 0 && <><p className="text-sm">Contents of these links were not captured:</p>
          <ul className="space-y-1 text-xs">{document.links.map((link, i) => <li key={i} className="break-all">{link}</li>)}</ul></>}
        {document.links_truncated && <p className="text-sm">Some link references were omitted.</p>}
      </section>)}
    </div>}
  </div>;
}
