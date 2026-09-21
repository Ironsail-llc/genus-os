"use client";

import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiError, apiFetch } from "@/lib/api/client";
import { LibraryEntry, type Entry } from "./library-entry";

const API = "/api/bridge/api/sales/library";
type Packet = Pick<Entry, "kind" | "version" | "data">;
type Preview = { packet: Packet; content_hash: string };
function readFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("Could not read the selected packet"));
    reader.readAsText(file);
  });
}

export function LibraryPublication({ onChanged }: { onChanged: () => void | Promise<void> }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [reason, setReason] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [held, setHeld] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const sequence = useRef(0);

  async function load(file?: File) {
    const current = ++sequence.current;
    setPreview(null); setReviewed(false); setReason(""); setHeld(false); setError(null); setMessage(null);
    if (!file) return;
    if (file.size > 131072) { setError("Review packets must be at most 128 KiB."); return; }
    setBusy(true);
    try {
      const packet = JSON.parse(await readFile(file));
      const result = await apiFetch<Preview>(API + "/preview", { method: "POST", body: JSON.stringify(packet) });
      if (sequence.current === current) setPreview(result);
    } catch (e) { if (sequence.current === current) setError(`Packet could not be validated: ${String(e)}`); }
    finally { if (sequence.current === current) setBusy(false); }
  }
  async function publish() {
    if (!preview || held || busy || !reviewed || reason.trim().length < 10) return;
    setBusy(true); setError(null);
    try {
      await apiFetch(API + "/publication", { method: "POST", body: JSON.stringify({ packet: preview.packet, expected_hash: preview.content_hash, reason: reason.trim() }) });
      setHeld(true); setMessage("Reviewed version published. Select it separately when ready.");
      await onChanged();
    } catch (e) {
      setHeld(true);
      setError(`${String(e)} Check the published version before another publication attempt.`);
    } finally { setBusy(false); }
  }
  async function check() {
    if (!preview) return;
    setBusy(true); setError(null);
    try {
      const record = await apiFetch<Entry & { content_hash: string }>(`${API}/records/${preview.packet.kind}/${encodeURIComponent(preview.packet.version)}`, { cache: "no-store" });
      if (record.content_hash !== preview.content_hash) {
        setHeld(true); setError("This version already contains different content. Prepare a packet with a new version label."); return;
      }
      setHeld(true); setMessage("Publication confirmed. This does not select or activate the version.");
      await onChanged();
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setHeld(false); setReviewed(false);
        setError("No published record was found. Review the packet again before retrying. Publication never replaces an existing version.");
      } else { setError(`Could not confirm publication: ${String(e)}`); setHeld(true); }
    } finally { setBusy(false); }
  }
  return <section aria-label="Publish sales library" className="rounded border p-4 space-y-4">
    <h4 className="font-medium">Publish a reviewed policy or claim library</h4>
    <p className="text-sm text-muted-foreground">Load a versioned review packet, inspect its content and sources, and record your approval. Published versions are immutable. Publication and active selection are separate steps.</p>
    <label className="block space-y-1 text-sm"><span>Library review packet</span>
      <input type="file" accept=".json,application/json" disabled={busy} className="block w-full text-sm" onChange={(event) => void load(event.target.files?.[0])} />
    </label>
    {error && <p role="alert" className="rounded border border-destructive p-3 text-sm text-destructive">{error}</p>}
    {message && <p role="status" className="text-sm">{message}</p>}
    {preview && <>
      <LibraryEntry entry={preview.packet} published={false} />
      <label className="block space-y-1 text-sm"><span>Publication review reason</span><textarea className="w-full rounded border bg-transparent p-2" maxLength={2000} value={reason} disabled={busy || held} onChange={(event) => { setReason(event.target.value); setReviewed(false); }} /></label>
      <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={reviewed} disabled={busy || held} onChange={(event) => setReviewed(event.target.checked)} />I reviewed the rules or claims and supporting sources</label>
      <div className="flex flex-wrap gap-2">
        <Button disabled={busy || held || !reviewed || reason.trim().length < 10} onClick={() => void publish()}>Publish reviewed version</Button>
        <Button variant="outline" disabled={busy} onClick={() => void check()}>Check published version</Button>
      </div>
    </>}
  </section>;
}
