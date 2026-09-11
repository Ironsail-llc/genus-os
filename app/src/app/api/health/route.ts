import { NextResponse } from "next/server";
import { CORE_SERVICES, checkService, overallStatus } from "@/lib/services/health";
import { getConfiguredServiceUrl, getServiceHealthPath } from "@/lib/services/registry";

/**
 * Probe every core service at the path the service manifest declares for it.
 * Engine and bridge answer 401 on /health (it is behind their auth wall) and
 * publish an unauthenticated /ready with per-dependency checks; orchestrator
 * and vision publish /health. A service that reports itself switched off is
 * "disabled" and never degrades the overall status.
 */
export async function GET() {
  const services = await Promise.all(
    CORE_SERVICES.map((name) =>
      checkService(name, getConfiguredServiceUrl(name, getServiceHealthPath(name))),
    ),
  );
  return NextResponse.json({
    status: overallStatus(services),
    services,
    timestamp: new Date().toISOString(),
  });
}
