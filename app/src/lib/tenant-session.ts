/**
 * Which tenant a session acts in.
 *
 * The acting tenant lives in the bridge access token (`tid`, with `home_tid`
 * and `act: true` while switched). The token reaches this code only from the
 * encrypted Auth.js cookie and the bridge verifies it on every request, so the
 * payload is DECODED here, never verified — it chooses which engine to ask,
 * and that engine verifies the same bearer before answering.
 */

export interface TenantClaims {
  tenant: string;
  homeTenant: string;
}

/** `tid` / `home_tid` from an `Authorization: Bearer <jwt>` value, or null. */
export function tenantClaimsFromAuthorization(authorization?: string | null): TenantClaims | null {
  const match = /^Bearer\s+([^.\s]+)\.([^.\s]+)\.[^.\s]*$/.exec(authorization ?? "");
  if (!match) return null;
  try {
    const b64 = match[2].replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const payload: unknown = JSON.parse(new TextDecoder().decode(bytes));
    if (!payload || typeof payload !== "object") return null;
    const { tid, home_tid } = payload as { tid?: unknown; home_tid?: unknown };
    if (typeof tid !== "string" || !tid) return null;
    return { tenant: tid, homeTenant: typeof home_tid === "string" && home_tid ? home_tid : tid };
  } catch {
    return null;
  }
}

/** The acting tenant's id when it differs from home, else null. */
export function actingTenantId(
  session: { tenantId?: string | null; homeTenantId?: string | null } | null | undefined,
): string | null {
  const tenant = session?.tenantId;
  const home = session?.homeTenantId;
  if (!tenant || !home || tenant === home) return null;
  return tenant;
}
