/**
 * GET /api/system/topology — Service dependency graph.
 */

import { NextResponse } from "next/server";
import { listServices } from "@/lib/services/registry";

export async function GET() {
  const services = listServices();

  const nodes = Object.entries(services).map(([key, svc]) => ({
    id: key,
    name: svc.name,
    port: svc.port,
    systemdUnit: svc.systemd_unit || null,
    tunnelRoute: svc.tunnel_route || null,
  }));

  const edges: Array<{ from: string; to: string }> = [];
  for (const [key, svc] of Object.entries(services)) {
    for (const dep of svc.dependencies || []) {
      edges.push({ from: dep, to: key });
    }
  }

  return NextResponse.json({
    nodes,
    edges,
    timestamp: new Date().toISOString(),
  });
}
