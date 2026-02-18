import { NextRequest, NextResponse } from "next/server";

const VISION_URL = "http://localhost:8600";

async function proxy(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> }
) {
  const { path } = await context.params;
  const target = `${VISION_URL}/${path.join("/")}${req.nextUrl.search}`;

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
      { error: "Vision service unavailable" },
      { status: 502 }
    );
  }
}

export const GET = proxy;
export const POST = proxy;
