"use client";

/**
 * "Owner accounts must enable two-factor authentication."
 *
 * Deliberately has no dismiss control. The banner shows only when the bridge
 * says this account is the owner, has no second factor, and local login is the
 * ONLY way into this appliance — at which point one password stands between
 * an attacker and every agent, credential and mailbox the instance can reach.
 * A "remind me later" on that is a button for never doing it.
 *
 * `/api/auth/me` is the authority (via the BFF proxy, which attaches the
 * session's bridge bearer). The session flag set at sign-in is only a hint,
 * and would go stale the moment the operator enrols.
 */

import { useEffect, useState } from "react";

export function MfaSetupBanner() {
  const [required, setRequired] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/bridge/api/auth/me");
        if (!res.ok) return;
        const body: unknown = await res.json();
        const flag = (body as { mfa_setup_required?: boolean } | null)?.mfa_setup_required;
        if (!cancelled) setRequired(flag === true);
      } catch {
        // A banner that cannot confirm the policy stays silent rather than
        // nagging an operator whose bridge is simply down.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!required) return null;

  return (
    <div
      role="alert"
      className="flex flex-wrap items-center justify-between gap-2 border-b border-amber-500/40 bg-amber-500/10 px-4 py-2 text-sm text-amber-900 dark:text-amber-200"
    >
      <span>
        Owner accounts must enable two-factor authentication. Email and password is currently the
        only way into this instance.
      </span>
      <a
        href="/account/security"
        className="shrink-0 rounded-md bg-amber-500/20 px-3 py-1 text-xs font-medium underline underline-offset-2 hover:bg-amber-500/30"
      >
        Set up two-factor
      </a>
    </div>
  );
}
