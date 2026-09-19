"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { apiFetch } from "@/lib/api/client";

const API = "/api/bridge/api/sales/settings";
type Snapshot = { config: Record<string, unknown>; revision: number };
type Review = { changes: Record<string, string | number | number[]>; expected_revision: number; reason: string };
type Field = { key: string; label: string; kind: "usd" | "integer" | "timezone" | "cadence" | "mode"; fallback: number | string | number[]; min?: number; max?: number };
const fields: Field[] = [
  { key: "monthly_limit_units", label: "Monthly spending limit (USD)", kind: "usd", fallback: 0 },
  { key: "daily_limit_units", label: "Daily spending limit (USD)", kind: "usd", fallback: 0 },
  { key: "verification_allowance_units", label: "Allowance per email verification (USD)", kind: "usd", fallback: 0, max: 1_000_000 },
  { key: "discovery_daily_limit", label: "New companies per day", kind: "integer", fallback: 20, min: 0, max: 1000 },
  { key: "discovery_mode", label: "Discovery scheduling", kind: "mode", fallback: "scheduled" },
  { key: "review_backlog_limit", label: "Maximum review backlog", kind: "integer", fallback: 100, min: 1, max: 10000 },
  { key: "mailbox_daily_limit", label: "Emails per mailbox per day", kind: "integer", fallback: 5, min: 0, max: 100 },
  { key: "followup_delays_business_days", label: "Follow-up delays (business days; blank disables)", kind: "cadence", fallback: [] },
  { key: "discovery_start_hour", label: "Discovery starts at hour", kind: "integer", fallback: 2, min: 0, max: 23 },
  { key: "discovery_end_hour", label: "Discovery ends at hour", kind: "integer", fallback: 7, min: 1, max: 24 },
  { key: "timezone", label: "Timezone", kind: "timezone", fallback: "America/Chicago" },
];

function display(field: Field, value: unknown) {
  const current = value ?? field.fallback;
  if (field.kind === "cadence") return Array.isArray(current) ? current.join(", ") : "";
  return field.kind === "usd" ? (Number(current) / 1e6).toFixed(6).replace(/\.?0+$/, "") || "0" : String(current);
}

function parse(field: Field, raw: string): string | number | number[] {
  const value = raw.trim();
  if (field.kind === "mode") {
    if (value !== "scheduled" && value !== "requests") throw new Error("Select a supported discovery mode.");
    return value;
  }
  if (field.kind === "cadence") {
    if (!value) return [];
    const parts = value.split(",").map((part) => part.trim());
    if (parts.length > 2 || parts.some((part) => !/^\d+$/.test(part) || Number(part) < 1 || Number(part) > 30)) {
      throw new Error("Follow-ups: enter at most two comma-separated delays of 1–30 business days.");
    }
    return parts.map(Number);
  }
  if (field.kind === "timezone") {
    if (!value) throw new Error("Timezone is required.");
    try { new Intl.DateTimeFormat("en-US", { timeZone: value }); }
    catch { throw new Error("Use a valid IANA timezone, such as America/New_York."); }
    return value;
  }
  let parsed: number;
  if (field.kind === "usd") {
    if (!/^(0|[1-9]\d*)(\.\d{1,6})?$/.test(value)) throw new Error(`${field.label}: use a nonnegative amount with at most six decimal places.`);
    const [whole, fraction = ""] = value.split(".");
    parsed = Number(whole) * 1_000_000 + Number(fraction.padEnd(6, "0"));
  } else {
    if (!/^\d+$/.test(value)) throw new Error(`${field.label}: use a whole number.`);
    parsed = Number(value);
  }
  if (!Number.isSafeInteger(parsed) || parsed < (field.min ?? 0) || parsed > (field.max ?? Number.MAX_SAFE_INTEGER)) {
    throw new Error(`${field.label}: amount is outside the supported range.`);
  }
  return parsed;
}

export function PilotSettings({ onChanged }: { onChanged: () => void | Promise<void> }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState(false);
  const [held, setHeld] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  function accept(current: Snapshot) {
    setSnapshot(current);
    setValues(Object.fromEntries(fields.map((field) => [field.key, display(field, current.config[field.key])])));
    setReview(null); setReason(""); setHeld(false);
  }
  useEffect(() => {
    let active = true;
    apiFetch<Snapshot>(API, { cache: "no-store" }).then((current) => { if (active) accept(current); })
      .catch((e) => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, []);

  async function reload() {
    setBusy(true); setError(null); setSaved(false);
    try { accept(await apiFetch<Snapshot>(API, { cache: "no-store" })); }
    catch (e) { setError(String(e)); setHeld(true); }
    finally { setBusy(false); }
  }
  function inspect() {
    if (!snapshot) return;
    setError(null); setSaved(false); setReview(null);
    try {
      const parsed = Object.fromEntries(fields.map((field) => [field.key, parse(field, values[field.key]) ]));
      if (Number(parsed.discovery_start_hour) >= Number(parsed.discovery_end_hour)) throw new Error("Discovery must start before it ends in the same local day.");
      const changes = Object.fromEntries(fields.filter((field) => JSON.stringify(parsed[field.key]) !== JSON.stringify(snapshot.config[field.key] ?? field.fallback)).map((field) => [field.key, parsed[field.key]]));
      if (!Object.keys(changes).length) throw new Error("Change at least one limit before review.");
      if (reason.trim().length < 10) throw new Error("Explain the change in at least 10 characters.");
      setReview({ changes, expected_revision: snapshot.revision, reason: reason.trim() });
    } catch (e) { setError(String(e)); }
  }
  async function save() {
    if (!review || busy || held) return;
    setBusy(true); setError(null);
    let acknowledged = false;
    try {
      const current = await apiFetch<Snapshot>(API + "/review", { method: "POST", body: JSON.stringify(review) });
      acknowledged = true; accept(current); setSaved(true);
      await onChanged();
    } catch (e) {
      setHeld(true); setReview(null);
      setError(`${acknowledged ? "Limits saved, but the workspace could not refresh." : String(e)} Reload current limits and review again before another save.`);
    } finally { setBusy(false); }
  }
  return <section aria-label="Pilot settings" className="rounded-lg border p-4 space-y-4">
    <div className="flex flex-wrap justify-between items-center gap-3">
      <h3 className="font-medium">Pilot limits and discovery hours</h3>
      <Button variant="outline" disabled={busy} onClick={() => void reload()}>Reload current limits</Button>
    </div>
    <p className="text-sm text-muted-foreground">Discovery runs on weekdays within these local hours and stops at the review backlog limit. The mailbox cap includes initial emails, follow-ups and replies. Saving limits leaves integration switches unchanged.</p>
    <p className="text-sm text-muted-foreground">Verification allowance is a cost ceiling per lookup. Set it from your subscription pricing; zero leaves paid verification paused.</p>
    <p className="text-sm text-muted-foreground">Each follow-up delay starts from the preceding confirmed send and counts Monday through Friday. For example, 3, 4 means wait three business days, then four more after the first follow-up is sent. Every message needs its own approval; any inbound message stops this sequence.</p>
    {error && <p role="alert" className="rounded border border-destructive p-3 text-sm text-destructive">{error}</p>}
    {saved && <p role="status" className="text-sm">Reviewed limits saved.</p>}
    {!snapshot ? <p>Loading current limits…</p> : <>
      <div className="grid gap-4 md:grid-cols-2">{fields.map((field) => <label key={field.key} htmlFor={`pilot-${field.key}`} className="block space-y-1 text-sm">
        <span>{field.label}</span>{field.kind === "mode" ? <select id={`pilot-${field.key}`} className="block w-full rounded border p-2" disabled={busy || held} value={values[field.key] ?? "scheduled"} onChange={(event) => { setValues({ ...values, [field.key]: event.target.value }); setReview(null); setSaved(false); }}>
          <option value="scheduled">Requests plus scheduled segments</option><option value="requests">Bounded requests only</option>
        </select> : <Input id={`pilot-${field.key}`} disabled={busy || held} value={values[field.key] ?? ""}
          inputMode={field.kind === "usd" ? "decimal" : field.kind === "integer" ? "numeric" : "text"}
          onChange={(event) => { setValues({ ...values, [field.key]: event.target.value }); setReview(null); setSaved(false); }} />}
      </label>)}</div>
      <label htmlFor="pilot-reason" className="block space-y-1 text-sm"><span>Reason for these limits</span>
        <textarea id="pilot-reason" className="w-full rounded border bg-transparent p-2" maxLength={2000} value={reason} disabled={busy || held}
          onChange={(event) => { setReason(event.target.value); setReview(null); }} />
      </label>
      <Button variant="outline" disabled={busy || held} onClick={inspect}>Review changes</Button>
      {review && <section aria-label="Review pilot changes" className="rounded border p-3 space-y-3">
        <h4 className="font-medium">Review pilot changes</h4>
        <ul className="space-y-2 text-sm">{fields.filter((field) => field.key in review.changes).map((field) => <li key={field.key} className="break-words">
          {field.label}: {display(field, snapshot.config[field.key])} → {display(field, review.changes[field.key])}
        </li>)}</ul>
        <p className="text-sm">{review.reason}</p>
        <Button disabled={busy || held} onClick={() => void save()}>Save reviewed limits</Button>
      </section>}
    </>}
  </section>;
}
