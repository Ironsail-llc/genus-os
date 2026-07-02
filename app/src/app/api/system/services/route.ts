/**
 * GET /api/system/services — List all services with live health status.
 */

import { NextResponse } from "next/server";
import { listServices, getServiceUrl, getHealthUrl } from "@/lib/services/registry";

interface ServiceStatus {
  name: string;
  port: number;
  url: string | null;
  healthUrl: string | null;
  status: "healthy" | "unhealthy" | "unknown";
  responseTime?: number;
  tunnelRoute?: string | null;
  systemdUnit?: string | null;
  dependencies: string[];
}

export async function GET() {
  const services = listServices();
  const results: ServiceStatus[] = [];

  const checks = Object.entries(services).map(async ([key, svc]) => {
    const healthUrl = getHealthUrl(key);
    let status: "healthy" | "unhealthy" | "unknown" = "unknown";
    let responseTime: number | undefined;

    const serviceUrl = getServiceUrl(key);
    if (healthUrl && serviceUrl) {
      const start = Date.now();
      try {
        // Assert the health URL resolves to the trusted service origin
        // before fetching (breaks SSRF/file-access-to-http taint flow).
        const base = new URL(serviceUrl);
        const target = new URL(healthUrl, base.origin);
        if (target.origin !== base.origin) {
          throw new Error("Health URL origin mismatch");
        }
        const res = await fetch(target.toString(), {
          signal: AbortSignal.timeout(5000),
        });
        responseTime = Date.now() - start;
        status = res.status < 500 ? "healthy" : "unhealthy";
      } catch {
        responseTime = Date.now() - start;
        status = "unhealthy";
      }
    }

    results.push({
      name: key,
      port: svc.port,
      url: serviceUrl,
      healthUrl,
      status,
      responseTime,
      tunnelRoute: svc.tunnel_route,
      systemdUnit: svc.systemd_unit,
      dependencies: svc.dependencies || [],
    });
  });

  await Promise.allSettled(checks);

  // Sort by name
  results.sort((a, b) => a.name.localeCompare(b.name));

  const healthy = results.filter((s) => s.status === "healthy").length;
  const unhealthy = results.filter((s) => s.status === "unhealthy").length;

  return NextResponse.json({
    total: results.length,
    healthy,
    unhealthy,
    unknown: results.length - healthy - unhealthy,
    services: results,
    timestamp: new Date().toISOString(),
  });
}
