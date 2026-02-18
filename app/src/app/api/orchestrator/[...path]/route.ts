import { NextRequest, NextResponse } from "next/server";

const ORCHESTRATOR_URL = "http://localhost:9099";

async function proxy(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> }
) {
  const { path } = await context.params;
  const target = `${ORCHESTRATOR_URL}/${path.join("/")}${req.nextUrl.search}`;

  try {
    const headers: Record<string, string> = {
      "Content-Type": req.headers.get("content-type") || "application/json",
    };

    const res = await fetch(target, {
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
