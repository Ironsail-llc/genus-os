/**
 * `/setup` — the first-run wizard, and the only page that renders without a
 * session.
 *
 * A server component wrapper around the client stepper for one reason: the
 * page must be 404 on an instance that already has an owner, and that decision
 * belongs to the bridge rather than to the browser. `GET /api/setup/status`
 * answers 200 while the setup router is live and 404 once it is gone, so a
 * non-200 here is `notFound()` — the same answer the middleware gives one hop
 * earlier, and the same one the bridge gives one hop later. Three refusals,
 * because this page can create the account that owns the appliance.
 *
 * The token from the query string is passed to the client component and is
 * exchanged once for a claim; the stepper then clears it out of the address
 * bar. It is never put in `localStorage`, never sent to any other origin, and
 * never rendered.
 */

import { notFound } from "next/navigation";

import { getServiceUrl } from "@/lib/services/registry";

import { SetupWizard } from "./setup-wizard";

export const dynamic = "force-dynamic";

async function setupIsLive(): Promise<boolean> {
  const bridge = getServiceUrl("bridge") || "http://localhost:9100";
  try {
    const res = await fetch(`${bridge}/api/setup/status`, {
      cache: "no-store",
      signal: AbortSignal.timeout(3_000),
    });
    if (!res.ok) return false;
    const payload = (await res.json()) as { complete?: boolean } | null;
    return payload?.complete === false;
  } catch {
    // An unreachable bridge is not a reason to render a first-run wizard: the
    // page would then offer to create an owner it cannot create.
    return false;
  }
}

export default async function SetupPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  if (!(await setupIsLive())) notFound();

  const params = await searchParams;
  const raw = params.token;
  const token = typeof raw === "string" ? raw : "";

  return <SetupWizard initialToken={token} />;
}
