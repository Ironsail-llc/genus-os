import { NextResponse } from "next/server";

import { checkDashboardAuthConfig, checkService } from "@/lib/services/health";
import { getConfiguredServiceUrl } from "@/lib/services/registry";

/** Dependency-aware readiness used by Kubernetes and deployment smoke tests. */
export async function GET() {
  const services = await Promise.all([
    Promise.resolve(checkDashboardAuthConfig()),
    checkService("engine", getConfiguredServiceUrl("engine", "/ready")),
    checkService("bridge", getConfiguredServiceUrl("bridge", "/ready")),
    checkService("orchestrator", getConfiguredServiceUrl("orchestrator", "/ready")),
  ]);
  const ready = services.every((service) => service.status === "healthy");
  const publicServices = services.map(({ name, status, responseTime }) => ({
    name,
    status,
    responseTime,
  }));

  return NextResponse.json(
    {
      status: ready ? "ok" : "degraded",
      service: "dashboard",
      services: publicServices,
      timestamp: new Date().toISOString(),
    },
    { status: ready ? 200 : 503 }
  );
}
