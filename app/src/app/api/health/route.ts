import { NextResponse } from "next/server";

interface ServiceHealth {
  name: string;
  url: string;
  status: "healthy" | "unhealthy";
  responseTime?: number;
}

async function checkService(
  name: string,
  url: string
): Promise<ServiceHealth> {
  const start = Date.now();
  try {
    const res = await fetch(url, {
      signal: AbortSignal.timeout(5000),
    });
    return {
      name,
      url,
      status: res.ok ? "healthy" : "unhealthy",
      responseTime: Date.now() - start,
    };
  } catch {
    return { name, url, status: "unhealthy", responseTime: Date.now() - start };
  }
}

export async function GET() {
  const services = await Promise.all([
    checkService("bridge", "http://localhost:9100/health"),
    checkService("orchestrator", "http://localhost:9099/health"),
    checkService("vision", "http://localhost:8600/health"),
  ]);

  const allHealthy = services.every((s) => s.status === "healthy");

  return NextResponse.json({
    status: allHealthy ? "ok" : "degraded",
    services,
    timestamp: new Date().toISOString(),
  });
}
