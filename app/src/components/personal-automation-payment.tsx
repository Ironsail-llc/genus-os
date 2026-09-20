"use client";

import { useEffect, useRef, useState } from "react";

type Payment = {
  event_count: number; reconciliation_required: boolean;
  position: null | {state: string; authorized_minor: number; charged_minor: number;
    refunded_minor: number; net_charged_minor: number};
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
        {payment.position && <>
          <p>{labels[payment.position.state] || "Payment status unresolved"}</p>
          {payment.position.authorized_minor > 0 && <p>Authorized: {money(payment.position.authorized_minor)}</p>}
          {payment.position.charged_minor > 0 && <>
            <p>Charged: {money(payment.position.charged_minor)}</p>
            <p>Refunded: {money(payment.position.refunded_minor)}</p>
            <p>Net charged: {money(payment.position.net_charged_minor)}</p>
          </>}
        </>}
        <p className="text-muted-foreground">Recorded refunds do not automatically change your spending allowance or cancel a membership.</p>
      </>}
    </div>}
  </div>;
}
