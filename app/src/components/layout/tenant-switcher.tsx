"use client";

/**
 * Acting in another tenant: the top-bar picker and the banner that says so.
 *
 * Both read the session's `tenantId` (acting) and `homeTenantId`, and the
 * bridge's `GET /api/tenants/accessible` (home first) through the BFF proxy.
 * A user with one accessible tenant never sees the picker. A switch goes
 * through `/api/session/switch-tenant`, which keeps the acting bearer in the
 * session cookie; on success the page reloads, so every view refetches as the
 * new tenant from that tenant's engine.
 *
 * UX only. The bridge authorizes the switch and every request after it.
 */

import { useEffect, useState } from "react";
import { useSession } from "next-auth/react";
import { Building2 } from "lucide-react";

import { NativeSelect } from "@/components/ui/native-select";
import { actingTenantId } from "@/lib/tenant-session";

export interface AccessibleTenant {
  id: string;
  display_name: string;
  parent_tenant_id: string | null;
  is_home: boolean;
}

type SwitchProps = {
  /** After a successful switch. Defaults to a full reload. */
  onSwitched?: () => void;
};

function reload() {
  window.location.reload();
}

function isTenantList(value: unknown): value is AccessibleTenant[] {
  return (
    Array.isArray(value) &&
    value.every(
      (t) => t && typeof t.id === "string" && typeof t.display_name === "string",
    )
  );
}

function useAccessibleTenants(enabled: boolean): AccessibleTenant[] | null {
  const [tenants, setTenants] = useState<AccessibleTenant[] | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/bridge/api/tenants/accessible", { cache: "no-store" });
        if (!res.ok) return;
        const body: unknown = await res.json();
        if (!cancelled && isTenantList(body)) setTenants(body);
      } catch {
        // No listing, no picker: a switcher that cannot list tenants offers nothing.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return tenants;
}

async function requestSwitch(tenant: string): Promise<string | null> {
  try {
    const res = await fetch("/api/session/switch-tenant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant }),
    });
    if (res.ok) return null;
    const body = (await res.json().catch(() => null)) as { error?: unknown } | null;
    return typeof body?.error === "string" && body.error ? body.error : `Switch failed (${res.status})`;
  } catch {
    return "Switch failed: the dashboard could not be reached";
  }
}

/** One switch at a time; a refusal is kept to show, a success hands off. */
function useTenantSwitch(onSwitched: () => void) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function switchTo(tenant: string) {
    setPending(true);
    setError(null);
    const refused = await requestSwitch(tenant);
    setPending(false);
    if (refused) setError(refused);
    else onSwitched();
  }
  return { pending, error, switchTo };
}

export function TenantSwitcher({ onSwitched = reload }: SwitchProps) {
  const { data: session } = useSession();
  const tenants = useAccessibleTenants(Boolean(session));
  const { pending, error, switchTo } = useTenantSwitch(onSwitched);

  if (!tenants || tenants.length <= 1) return null;
  const current = session?.tenantId ?? tenants.find((t) => t.is_home)?.id ?? tenants[0].id;

  function choose(tenant: string) {
    if (tenant !== current) void switchTo(tenant);
  }

  return (
    <span className="inline-flex items-center gap-2" data-testid="tenant-switcher">
      <Building2 aria-hidden className="size-3.5 text-muted-foreground" />
      <NativeSelect
        aria-label="Tenant"
        value={current}
        disabled={pending}
        onChange={(e) => choose(e.target.value)}
        className="h-7 text-xs max-w-48"
      >
        {tenants.map((t) => (
          <option key={t.id} value={t.id}>
            {t.display_name}
          </option>
        ))}
      </NativeSelect>
      {error && (
        <span role="alert" className="max-w-56 truncate text-xs text-destructive" title={error}>
          {error}
        </span>
      )}
    </span>
  );
}

export function ActingTenantBanner({ onSwitched = reload }: SwitchProps) {
  const { data: session, update } = useSession();
  const acting = actingTenantId(session);
  const tenants = useAccessibleTenants(acting !== null);
  const { pending, error, switchTo } = useTenantSwitch(onSwitched);
  const [notice, setNotice] = useState(false);

  // Shown once per notice: raised during render when the session first carries
  // it, then acknowledged so the cookie stops carrying it.
  const reverted = session?.tenantNotice === "acting-tenant-reverted";
  const [seenReverted, setSeenReverted] = useState(false);
  if (reverted !== seenReverted) {
    setSeenReverted(reverted);
    if (reverted) setNotice(true);
  }
  useEffect(() => {
    if (!reverted) return;
    void update({ tenantNoticeSeen: true });
    const timer = setTimeout(() => setNotice(false), 8_000);
    return () => clearTimeout(timer);
  }, [reverted, update]);

  function switchBack() {
    if (session?.homeTenantId) void switchTo(session.homeTenantId);
  }

  const name = acting ? (tenants?.find((t) => t.id === acting)?.display_name ?? acting) : null;

  return (
    <>
      {name && (
        <div
          data-testid="acting-tenant-banner"
          className="flex flex-wrap items-center justify-between gap-2 border-b border-primary/25 bg-primary/10 px-4 py-2 text-sm text-foreground"
        >
          <span className="inline-flex items-center gap-2 min-w-0">
            <Building2 aria-hidden className="size-3.5 shrink-0 text-primary" />
            <span className="truncate">Acting as {name}</span>
            {error && (
              <span role="alert" className="text-xs text-destructive">
                {error}
              </span>
            )}
          </span>
          <button
            type="button"
            onClick={switchBack}
            disabled={pending}
            className="shrink-0 rounded-md border border-primary/25 bg-background/60 px-3 py-1 text-xs font-medium transition-colors hover:bg-background disabled:opacity-50"
          >
            Switch back
          </button>
        </div>
      )}
      {notice && (
        <div
          role="status"
          className="fixed bottom-4 right-4 z-50 max-w-sm rounded-md border border-border bg-card px-4 py-3 text-sm text-foreground shadow-lg"
        >
          Your session was renewed and could not re-enter the tenant you were acting in. You are
          back in your home tenant.
        </div>
      )}
    </>
  );
}
