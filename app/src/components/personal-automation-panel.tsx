"use client";

import { useEffect, useRef, useState } from "react";
import { PersonalAutomationPayment } from "./personal-automation-payment";
import { PersonalAutomationAudit } from "./personal-automation-audit";

type Resource = { id: string; kind: string; label: string; origin?: string;
  descriptor?: { version?: number; fields?: string[]; source?: string } };
type Grant = { id: string; revoked: boolean; policy: { origins: string[]; allow_any_website?: boolean; allowed_purposes?: string[];
  verification_senders?: Record<string, string[]>;
  currency: string; per_purchase_minor: number; monthly_minor: number; recurring_minor: number; annual_minor: number } };
type Settings = { enabled: boolean; managed_browser: boolean; payment_processing: boolean;
  payment_assessment_reference: string };
type Status = { resources: Resource[]; grants: Grant[]; settings: Settings;
  spending?: { state: string; months: Record<string, Record<string, number>> } };
type Operation = { id: string; state: string; proposal: { purpose: string; origin: string; action: string; currency: string } };
const moneyDisplay = (minor: number, currency: string) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency }).format(minor / 100);
const sources: Record<string, string> = {
  secure_input: "Provided by you", linked_contact: "Imported from your linked contact",
  generated: "Generated for this website", broker_session: "Saved website session",
  mailbox_verification: "From a verified email", legacy_enrollment: "Previously saved; original source not recorded",
};
const inputClass = "w-full rounded border bg-background p-2";

function verificationSenders(value: FormDataEntryValue | null): Record<string, string[]> {
  const entries = String(value || "").split(/\n/).map(line => line.trim()).filter(Boolean).map(line => {
    const parts = line.split("=").map(part => part.trim());
    if (parts.length !== 2 || !parts[0] || !parts[1]) throw new Error("Enter each verification sender as website = sender domain.");
    return [parts[0], parts[1].split(",").map(domain => domain.trim())] as const;
  });
  if (new Set(entries.map(([website]) => website)).size !== entries.length) throw new Error("Combine sender domains for the same website on one line.");
  return Object.fromEntries(entries);
}

async function api(path: string, method = "GET", data?: unknown) {
  const response = await fetch(`/api/bridge/api/autonomy/${path}`, {
    method, headers: { "Content-Type": "application/json" }, cache: "no-store",
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "Request failed. Please try again.");
  return body;
}

export function PersonalAutomationPanel() {
  const intakeStarted = useRef(false);
  const intakeGeneration = useRef(0);
  const [intake, setIntake] = useState<{ token: string; origin: string | null } | null>(null);
  const [intakeState, setIntakeState] = useState("loading");
  useEffect(() => {
    function loadIntake() {
      const token = new URLSearchParams(window.location.hash.slice(1)).get("enroll");
      if (token === null) {
        if (!intakeStarted.current) setIntakeState("none");
        intakeStarted.current = true;
        return;
      }
      intakeStarted.current = true;
      const generation = ++intakeGeneration.current;
      setIntake(null); setIntakeState("loading"); setMessage("");
      window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search);
      api("enrollments/inspect", "POST", { token }).then(result => {
        if (generation !== intakeGeneration.current) return;
        if (result.resource_id) { setIntakeState("complete"); setMessage("This information has already been saved."); return; }
        setKind(result.kind); setIntake({ token, origin: result.origin }); setIntakeState("ready");
      }).catch(error => {
        if (generation !== intakeGeneration.current) return;
        setIntakeState("unavailable"); setMessage(error.message);
      });
    }
    loadIntake();
    window.addEventListener("hashchange", loadIntake);
    return () => window.removeEventListener("hashchange", loadIntake);
  }, []);
  const [status, setStatus] = useState<Status | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState("profile");
  const [answerCount, setAnswerCount] = useState(1);
  const [anyWebsite, setAnyWebsite] = useState(false);
  const [operations, setOperations] = useState<Operation[]>([]);

  async function refresh() {
    const [state, journal] = await Promise.all([api("status"), api("operations")]);
    setStatus(state); setOperations(journal.operations);
  }
  useEffect(() => {
    refresh().catch(e => setMessage(e.message));
    const timer = setInterval(() => { refresh().catch(() => {}); }, 5000);
    return () => clearInterval(timer);
  }, []);
  async function act(work: () => Promise<unknown>) {
    setBusy(true); setMessage("");
    try { await work(); await refresh(); setMessage("Saved."); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Unable to save."); }
    finally { setBusy(false); }
  }

  async function enroll(form: HTMLFormElement) {
    if (!["none", "ready"].includes(intakeState)) throw new Error("This enrollment is unavailable.");
    const fields = new FormData(form);
    const label = String(fields.get("label") || "");
    const origin = intake ? intake.origin : String(fields.get("origin") || "") || null;
    const payload: Record<string, string | number | Record<string, string>> = {};
    for (const [key, value] of fields) {
      if (!["label", "origin", "file"].includes(key) && !key.startsWith("answer_") && typeof value === "string" && value) {
        payload[key] = key === "expiry_month" || key === "expiry_year" ? Number(value) : value;
      }
    }
    if (kind === "profile") {
      const answers: Record<string, string> = {};
      for (let i = 0; i < answerCount; i++) {
        const name = String(fields.get(`answer_key_${i}`) || "").trim();
        const answer = String(fields.get(`answer_value_${i}`) || "");
        if (name && answer) answers[name] = answer;
      }
      if (Object.keys(answers).length) payload.answers = answers;
    }
    if (kind === "document") {
      const file = fields.get("file");
      if (!(file instanceof File) || !file.size || file.size > 5_000_000) {
        throw new Error("Choose a document or photo smaller than 5 MB.");
      }
      const encoded = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1]);
        reader.onerror = reject; reader.readAsDataURL(file);
      });
      Object.assign(payload, { name: file.name, mime_type: file.type || "application/octet-stream", base64: encoded });
    }
    const resource = { kind, label, origin, payload: JSON.stringify(payload) };
    if (intake) {
      await api("enrollments/complete", "POST", { token: intake.token, resource });
      setIntake(null); setIntakeState("complete");
    } else {
      await api("resources", "POST", resource);
    }
    form.reset();
  }

  const fields: Record<string, [string, string, string][]> = {
    profile: [["legal_name", "Legal name", "text"], ["first_name", "First name", "text"], ["last_name", "Last name", "text"],
      ["email", "Email", "email"], ["phone", "Phone", "tel"], ["date_of_birth", "Date of birth", "date"],
      ["address_line1", "Street address", "text"], ["address_line2", "Apartment / suite", "text"], ["city", "City", "text"], ["region", "State / region", "text"],
      ["postal_code", "Postal code", "text"], ["country", "Country", "text"], ["nationality", "Nationality", "text"],
      ["occupation", "Occupation", "text"], ["employer", "Employer", "text"], ["interests", "Interests", "text"]],
    credential: [["username", "Username or email", "text"], ["password", "Password", "password"]],
    payment_card: [["number", "Card number", "password"], ["name", "Name on card", "text"],
      ["expiry_month", "Expiry month", "number"], ["expiry_year", "Expiry year", "number"]],
    totp: [["secret", "Authenticator setup key", "password"]], document: [],
  };

  return <div className="space-y-8">
    {message && <p role="status" className="rounded border p-3">{message}</p>}
    {operations.length > 0 && <section className="space-y-3">
      <h2 className="text-lg font-medium">Recent tasks</h2>
      {operations.slice(0,10).map(operation => <div key={operation.id} className="space-y-2 rounded border p-3">
        <p>{operation.proposal.purpose} · {operation.state.replaceAll("_", " ")}</p>
        <p className="text-sm text-muted-foreground">{operation.proposal.origin}</p>
        <PersonalAutomationAudit operationId={operation.id} includeReceipts={["purchase", "subscription"].includes(operation.proposal.action)} />
        {["purchase", "subscription"].includes(operation.proposal.action) && <PersonalAutomationPayment key={operation.id} operationId={operation.id} currency={operation.proposal.currency} />}
        {operation.state === "awaiting_input" && <form className="flex gap-2" autoComplete="off" onSubmit={event => {
          event.preventDefault(); const form = event.currentTarget;
          const code = String(new FormData(form).get("code") || "");
          void act(async () => { await api(`operations/${operation.id}/verification`, "POST", { verification_code: code }); form.reset(); });
        }}>
          <input className={inputClass} name="code" type="password" inputMode="numeric" autoComplete="off"
            aria-label="Merchant verification code" placeholder="Verification code" required minLength={3} maxLength={6} />
          <button disabled={busy} type="submit">Continue</button>
        </form>}
      </div>)}
    </section>}
    <section className="space-y-3">
      <h2 className="text-lg font-medium">Your information</h2>
      <p className="text-sm text-muted-foreground">Saved values are private. Your assistant can use them for authorized tasks.</p>
      <button type="button" className="rounded border px-3 py-2" disabled={busy || !status}
        onClick={() => void act(() => api("profile-from-contact", "POST", {}))}>Use my saved contact details</button>
      <select aria-label="Information type" className={inputClass} value={kind} disabled={intakeState !== "none"} onChange={e => setKind(e.target.value)}>
        <option value="profile">Personal profile</option><option value="credential">Website login</option>
        <option value="payment_card">Payment card</option><option value="document">Photo or document</option>
        <option value="totp">Website authenticator</option>
      </select>
      <form key={`${kind}:${intake?.token || intakeState}`} className="space-y-3" autoComplete="off" onSubmit={event => {
        event.preventDefault(); const form = event.currentTarget; void act(() => enroll(form));
      }}>
        <label className="block">Label<input className={inputClass} name="label" required maxLength={100} /></label>
        {kind === "credential" || kind === "totp" ?
          <label className="block">Website<input className={inputClass} name="origin" type="url" placeholder="https://example.com" defaultValue={intake?.origin || ""} readOnly={!!intake} required /></label> : null}
        {fields[kind].map(([name, title, type]) => <label className="block" key={name}>{title}
          <input className={inputClass} name={name} type={type} required={kind !== "profile"} autoComplete="off" />
        </label>)}
        {kind === "document" && <input aria-label="Photo or document" name="file" type="file" required />}
        {kind === "profile" && <fieldset className="space-y-2">
          <legend>Reusable application answers</legend>
          {Array.from({ length: answerCount }, (_, i) => <div className="space-y-1" key={i}>
            <label className="block">Short name<input className={inputClass} name={`answer_key_${i}`}
              placeholder="membership_reason" pattern="[a-zA-Z][a-zA-Z0-9_]{0,59}" /></label>
            <label className="block">Answer<textarea className={inputClass} name={`answer_value_${i}`} maxLength={5000} /></label>
          </div>)}
          <button type="button" disabled={answerCount >= 80} onClick={() => setAnswerCount(answerCount + 1)}>Add another answer</button>
        </fieldset>}
        {kind === "payment_card" && <p className="text-sm">A bank or merchant may request verification when the card is used.</p>}
        <button className="rounded bg-primary px-4 py-2 text-primary-foreground" disabled={busy || !status || !["none", "ready"].includes(intakeState)}>Save information</button>
      </form>
      {status?.resources.some(resource => resource.descriptor?.version !== 1) && <button type="button"
        className="rounded border px-3 py-2" disabled={busy}
        onClick={() => void act(() => api("resources/refresh-descriptions", "POST", {}))}>Check saved information</button>}
      {status?.resources.map(resource => <div className="flex justify-between gap-3 rounded border p-3" key={resource.id}>
        <div><p>{resource.label} · {resource.kind.replaceAll("_", " ")}</p>
          {resource.descriptor?.fields && <p className="text-sm">Saved fields: {resource.descriptor.fields.map(field => field.replaceAll("_", " ")).join(", ") || "None"}</p>}
          {resource.descriptor?.source && <p className="text-sm text-muted-foreground">{sources[resource.descriptor.source] || "Saved information"}</p>}
        </div>
        <button disabled={busy} onClick={() => void act(() => api(`resources/${resource.id}`, "DELETE"))}>Revoke</button>
      </div>)}
    </section>
    <section className="space-y-3">
      <h2 className="text-lg font-medium">Standing authority</h2>
      <p className="text-sm">Your assistant can finish accounts, applications, and purchases on these websites without asking again, within your limits.</p>
      <form className="space-y-3" onSubmit={event => {
        event.preventDefault(); const data = new FormData(event.currentTarget);
        const money = (key: string) => Math.round(Number(data.get(key) || 0) * 100);
        void act(() => api("grants", "POST", { agent_ids: String(data.get("agents")).split(",").map(s => s.trim()),
          origins: String(data.get("origins") || "").split(/[,\n]/).map(s => s.trim()).filter(Boolean),
          allow_any_website: anyWebsite,
          allowed_purposes: String(data.get("purposes") || "").split(/\n/).map(s => s.trim()).filter(Boolean),
          frame_origins: String(data.get("frames") || "").split(/[,\n]/).map(s => s.trim()).filter(Boolean),
          verification_senders: verificationSenders(data.get("verification_senders")),
          actions: ["account", "login", "application", "purchase", "subscription"], currency: "USD",
          expires_at: new Date(String(data.get("expires")) + "T23:59:59Z").toISOString(),
          per_purchase_minor: money("purchase"), monthly_minor: money("monthly"),
          recurring_minor: money("recurring"), annual_minor: money("annual") }));
      }}>
        <label className="block">Agent IDs<input name="agents" className={inputClass} defaultValue="main" required /></label>
        <label className="flex gap-2"><input type="checkbox" checked={anyWebsite}
          onChange={event => setAnyWebsite(event.target.checked)} />Allow any public HTTPS website</label>
        <label className="block">Websites, one per line<textarea name="origins" className={inputClass}
          placeholder="https://example.com" required={!anyWebsite} disabled={anyWebsite} /></label>
        <label className="block">Embedded payment providers, if needed<textarea name="frames" className={inputClass} placeholder="https://payments.example.com" /></label>
        <label className="block">Additional verification senders<textarea name="verification_senders" className={inputClass}
          maxLength={24080} placeholder="https://shop.example = mail.provider.example" /></label>
        <p className="text-sm text-muted-foreground">Optional: authorize an external email sender for a specific website. Enter website = sender domain, one website per line; separate multiple sender domains with commas. The website’s own domain is already supported.</p>
        <label className="block">Allowed purposes, one per line<textarea name="purposes" className={inputClass}
          maxLength={24080} placeholder="Personal memberships" /></label>
        <p className="text-sm text-muted-foreground">Leave empty for any task covered by this grant.</p>
        <label className="block">Authority expires<input type="date" name="expires" className={inputClass} required /></label>
        {[["purchase", "Per purchase"], ["monthly", "Monthly total"], ["recurring", "Per recurring charge"], ["annual", "Annual commitment per membership"]].map(([key, title]) =>
          <label className="block" key={key}>{title} (USD)<input className={inputClass} type="number" name={key} min="0" step="0.01" defaultValue="0" required /></label>)}
        <button className="rounded bg-primary px-4 py-2 text-primary-foreground" disabled={busy || !status}>Grant authority</button>
      </form>
      {status?.grants.filter(grant => !grant.revoked).map(grant => <div className="flex justify-between rounded border p-3" key={grant.id}>
        <div><p>{grant.policy.allow_any_website ? "Any public HTTPS website" : grant.policy.origins.join(", ")}</p>
          <p className="text-sm">Purposes: {grant.policy.allowed_purposes?.length ? grant.policy.allowed_purposes.join(" · ") : "Any task covered by this grant"}</p>
          {Object.entries(grant.policy.verification_senders || {}).map(([website, domains]) =>
            <p className="text-sm" key={website}>Verification for {website}: {domains.join(", ")}</p>)}
          <p className="text-sm">Per purchase: {moneyDisplay(grant.policy.per_purchase_minor, grant.policy.currency)} · Monthly total: {moneyDisplay(grant.policy.monthly_minor, grant.policy.currency)}</p>
          <p className="text-sm">Per recurring charge: {moneyDisplay(grant.policy.recurring_minor, grant.policy.currency)} · Annual commitment: {moneyDisplay(grant.policy.annual_minor, grant.policy.currency)}</p>
        </div><button disabled={busy} onClick={() => void act(() => api(`grants/${grant.id}`, "DELETE"))}>Revoke</button>
      </div>)}
    </section>
    {status?.spending && Object.keys(status.spending.months).length > 0 && <section className="space-y-3">
      <h2 className="text-lg font-medium">Projected charges</h2>
      <p className="text-sm">Includes submitted purchases, pending tasks and recorded renewals. These are estimates, not bank settlement records. Merchants can charge saved cards independently of Robothor.</p>
      {Object.entries(status.spending.months).map(([currency, months]) => <details key={currency}>
        <summary>Monthly projection ({currency})</summary>
        <table className="w-full text-sm"><thead><tr><th className="text-left">Month (UTC)</th><th className="text-right">Projected total</th></tr></thead>
          <tbody>{Object.entries(months).map(([month, amount]) => <tr key={month}>
            <td>{month}</td><td className="text-right">{moneyDisplay(amount, currency)}</td>
          </tr>)}</tbody></table>
      </details>)}
    </section>}
    {status?.spending?.state === "renewal_schedule_missing" && <p role="status">A membership is missing its renewal schedule. New spending is paused until its dates are recorded.</p>}
    {status && <section className="space-y-3">
      <h2 className="text-lg font-medium">Execution</h2>
      <label className="flex gap-2"><input type="checkbox" checked={status.settings.enabled} disabled={busy}
        onChange={e => void act(() => api("settings", "PUT", { ...status.settings, enabled: e.target.checked }))} />Enable authorized tasks</label>
      <label className="flex gap-2"><input type="checkbox" checked={status.settings.managed_browser} disabled={busy}
        onChange={e => void act(() => api("settings", "PUT", { ...status.settings, managed_browser: e.target.checked }))} />Allow the configured managed browser when needed</label>
      <p className="text-sm">Payments: {status.settings.payment_processing ? "enabled" : "awaiting deployment setup"}</p>
    </section>}
  </div>;
}
