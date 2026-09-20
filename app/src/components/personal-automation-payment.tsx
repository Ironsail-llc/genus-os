"use client";

import { useEffect, useRef, useState } from "react";

type Position = {state: string; authorized_minor: number; charged_minor: number;
  refunded_minor: number; net_charged_minor: number; reversed_minor?: number; authorization_open_minor?: number};
type Payment = {
  event_count: number; reconciliation_required: boolean;
  position: Position | null;
  renewals?: {id: string; due_on: string | null; schedule_matches: boolean;
    period_limit_exceeded: boolean; reconciliation_required: boolean; position: Position | null}[];
};
const labels: Record<string, string> = {
  unconfirmed: "No verified payment evidence",
  submitted: "Checkout submitted; charge not verified",
  authorized: "Payment authorized; charge not verified",
  charged: "Charge recorded", partially_refunded: "Partial refund recorded",
  refunded: "Full refund recorded", reversed: "Authorization reversed",
};

export function PersonalAutomationPayment({ operationId, currency }: {operationId: string; currency: string}) {
  const [open, setOpen] = useState(false);
  const [payment, setPayment] = useState<Payment | null>(null);
  const [error, setError] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const money = (minor: number) => new Intl.NumberFormat("en-US", {style: "currency", currency}).format(minor / 100);
  async function toggle() {
    request.current?.abort();
    setPayment(null); setError(false); setOpen(!open);
    if (open) return;
    const controller = new AbortController(); request.current = controller;
    try {
      const response = await fetch(`/api/bridge/api/autonomy/operations/${operationId}/payment`, {cache: "no-store", signal: controller.signal});
      if (!response.ok) throw new Error("unavailable");
      const value: Payment = await response.json();
      if (!controller.signal.aborted) setPayment(value);
    } catch {
      if (!controller.signal.aborted) setError(true);
    }
  }
  return <div>
    <button type="button" className="text-sm underline" onClick={() => void toggle()}>{open ? "Close payment status" : "Payment status"}</button>
    {open && <div className="mt-2 space-y-1 text-sm" aria-live="polite">
      {error ? <p>Payment status unavailable. Close and reopen to retry.</p> : !payment ? <p>Loading payment status…</p> : <>
        {payment.reconciliation_required && <p>Payment evidence needs reconciliation. The recorded amounts may be incomplete or inconsistent.</p>}
        {!!payment.renewals?.length && <p className="font-medium">Initial payment</p>}
        {payment.position && <PaymentAmounts position={payment.position} money={money} />}
        {payment.renewals?.map(renewal => <div key={renewal.id} className="mt-2 border-t pt-2">
          <p className="font-medium">{renewal.due_on ? `Renewal due ${renewal.due_on}` : "Renewal date unresolved"}</p>
          {renewal.reconciliation_required && <p>This renewal needs reconciliation.</p>}
          {!renewal.schedule_matches && <p>The recorded billing date does not match the agreed renewal schedule.</p>}
          {renewal.period_limit_exceeded && <p>Recorded charges exceed the recurring allowance for this billing period.</p>}
          {renewal.position && <PaymentAmounts position={renewal.position} money={money} />}
        </div>)}
        <p className="text-muted-foreground">Recorded refunds and authorization releases do not automatically change your spending allowance or cancel a membership.</p>
      </>}
    </div>}
  </div>;
}

function PaymentAmounts({position, money}: {position: Position; money: (minor: number) => string}) {
  return <>
    <p>{labels[position.state] || "Payment status unresolved"}</p>
    {position.authorized_minor > 0 && <p>Authorized: {money(position.authorized_minor)}</p>}
    {(position.reversed_minor ?? 0) > 0 && <p>Authorization released: {money(position.reversed_minor ?? 0)}</p>}
    {position.authorized_minor > 0 && position.authorization_open_minor !== undefined && <p>Authorization remaining: {money(position.authorization_open_minor)}</p>}
    {position.charged_minor > 0 && <>
      <p>Charged: {money(position.charged_minor)}</p>
      <p>Refunded: {money(position.refunded_minor)}</p>
      <p>Net charged: {money(position.net_charged_minor)}</p>
    </>}
  </>;
}
