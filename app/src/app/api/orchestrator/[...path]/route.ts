import { NextRequest, NextResponse } from "next/server";

import { getServiceUrl } from "@/lib/services/registry";
const ORCHESTRATOR_URL = getServiceUrl("orchestrator") || "http://localhost:9099";

async function proxy(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> }
) {
  const { path } = await context.params;
  const base = new URL(ORCHESTRATOR_URL);
  const target = new URL(
    path.join("/").replace(/^\/+/, ""),
    base.origin + base.pathname.replace(/\/?$/, "/")
  );
  target.search = req.nextUrl.search;
  if (target.origin !== base.origin) {
    return new NextResponse("Bad gateway path", { status: 502 });
  }

  try {
    const headers: Record<string, string> = {
      "Content-Type": req.headers.get("content-type") || "application/json",
    };

    const res = await fetch(target.toString(), {
      method: req.method,
      headers,
      body: ["POST", "PUT", "PATCH"].includes(req.method)
        ? await req.text()
        : undefined,
      signal: AbortSignal.timeout(30000),
    });

    const contentType = res.headers.get("content-type") || "";
    const body = contentType.includes("json")
      ? await res.json()
      : await res.text();

    return contentType.includes("json")
      ? NextResponse.json(body, { status: res.status })
      : new NextResponse(body as string, { status: res.status });
  } catch {
    return NextResponse.json(
      { error: "Orchestrator service unavailable" },
      { status: 502 }
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
