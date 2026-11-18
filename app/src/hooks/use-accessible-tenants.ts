"use client";

/**
 * The tenants this person may act in (`GET /api/tenants/accessible`, home
 * first), read once per page through the shared query cache: the tenant
 * picker, the "Acting as" banner and the command centre's nav entry all ask
 * the same question.
 *
 * UX only. The bridge decides every switch and every read after it.
 */

import { useQuery } from "@tanstack/react-query";
import { useSession } from "next-auth/react";

import { isOperatorRole } from "@/components/layout/nav-config";
import { useAppQueryClient } from "@/lib/query/query-client";
import { actingTenantId } from "@/lib/tenant-session";

export interface AccessibleTenant {
  id: string;
  display_name: string;
  parent_tenant_id: string | null;
  is_home: boolean;
}

const ACCESSIBLE_KEY = ["bridge", "tenants", "accessible"] as const;
/** The hierarchy changes when a root operator reshapes it, which is rare. */
const ACCESSIBLE_STALE_MS = 5 * 60_000;

function isTenantList(value: unknown): value is AccessibleTenant[] {
  return (
    Array.isArray(value) &&
    value.every((t) => t && typeof t.id === "string" && typeof t.display_name === "string")
  );
}

/** No listing (refused, unreachable, malformed) is an empty one: nothing to offer. */
async function fetchAccessible(): Promise<AccessibleTenant[]> {
  try {
    const res = await fetch("/api/bridge/api/tenants/accessible", { cache: "no-store" });
    if (!res.ok) return [];
    const body: unknown = await res.json();
    return isTenantList(body) ? body : [];
  } catch {
    return [];
  }
}

/** The listing, or null until it has been read (or while `enabled` is false). */
export function useAccessibleTenants(enabled: boolean): AccessibleTenant[] | null {
  const client = useAppQueryClient();
  const query = useQuery(
    {
      queryKey: ACCESSIBLE_KEY,
      queryFn: fetchAccessible,
      enabled,
      staleTime: ACCESSIBLE_STALE_MS,
    },
    client,
  );
  return enabled ? (query.data ?? null) : null;
}

/**
 * Whether the signed-in account is an owner or admin, at home, whose tenant
 * has child tenants: who the command centre is for. Only owners and admins
 * are given descendants by the bridge, so nobody else is asked.
 */
export function useHasChildTenants(): boolean {
  const { data: session } = useSession();
  const eligible = Boolean(session) && isOperatorRole(session?.role) && !actingTenantId(session);
  const tenants = useAccessibleTenants(eligible);
  return eligible && (tenants?.length ?? 0) > 1;
}
