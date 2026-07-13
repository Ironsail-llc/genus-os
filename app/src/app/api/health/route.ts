import { NextResponse } from "next/server";

import { checkService } from "@/lib/services/health";
import { getServiceUrl } from "@/lib/services/registry";

export async function GET() {
  const services = await Promise.all([
    checkService("bridge", getServiceUrl("bridge", "/health")),
    checkService("orchestrator", getServiceUrl("orchestrator", "/health")),
    checkService("vision", getServiceUrl("vision", "/health")),
  ]);

  const allHealthy = services.every((s) => s.status === "healthy");

  return NextResponse.json({
    status: allHealthy ? "ok" : "degraded",
    services,
    timestamp: new Date().toISOString(),
  });
}
