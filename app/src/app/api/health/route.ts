import { NextResponse } from "next/server";

import { checkService } from "@/lib/services/health";
import { getConfiguredServiceUrl } from "@/lib/services/registry";

export async function GET() {
  const services = await Promise.all([
    checkService("bridge", getConfiguredServiceUrl("bridge", "/health")),
    checkService("orchestrator", getConfiguredServiceUrl("orchestrator", "/health")),
    checkService("vision", getConfiguredServiceUrl("vision", "/health")),
  ]);

  const allHealthy = services.every((s) => s.status === "healthy");

  return NextResponse.json({
    status: allHealthy ? "ok" : "degraded",
    services,
    timestamp: new Date().toISOString(),
  });
}
