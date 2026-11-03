/**
 * POST /api/session/switch-tenant {"tenant": "<id>"}
 *
 * Switches the caller's session to act in another tenant (its home tenant
 * restores). The bridge decides — role, ancestry and the server's
 * `GENUS_TENANT_SWITCH_MODE` flag — and the acting bearer it mints stays in
 * the Auth.js session cookie; the browser is told only which tenant it is in.
 */
import { NextResponse } from "next/server";

import { switchSessionTenant } from "@/lib/auth";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const MAX_TENANT_LENGTH = 255;
// The bridge's 503 body carries operator fix text naming an env override.
const ISOLATION_NOT_ENFORCING = "tenant switching unavailable: isolation not enforcing";

function requestedTenant(body: unknown): string | null {
  const tenant = (body as { tenant?: unknown } | null)?.tenant;
  if (typeof tenant !== "string") return null;
  const trimmed = tenant.trim();
  return trimmed && trimmed.length <= MAX_TENANT_LENGTH ? trimmed : null;
}

export async function POST(request: Request) {
  const body: unknown = await request.json().catch(() => null);
  const tenant = requestedTenant(body);
  if (!tenant) {
    return NextResponse.json({ error: "tenant required" }, { status: 400 });
  }

  const outcome = await switchSessionTenant(tenant);
  if (!outcome.ok) {
    const error = outcome.status === 503 ? ISOLATION_NOT_ENFORCING : outcome.error;
    return NextResponse.json({ error }, { status: outcome.status });
  }
  return NextResponse.json(
    { tenant: outcome.tenant, homeTenant: outcome.homeTenant },
    { headers: { "Cache-Control": "no-store" } },
  );
}
