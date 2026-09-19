import { NextRequest, NextResponse } from "next/server";

import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { isDeniedBridgePath } from "@/lib/bridge-proxy-policy";
import { getServiceUrl } from "@/lib/services/registry";
const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";

async function proxy(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> }
) {
  const { path } = await context.params;
  const base = new URL(BRIDGE_URL);
  const pathStr = path.join("/");
  const target = new URL(
    pathStr.replace(/^\/+/, ""),
    base.origin + base.pathname.replace(/\/?$/, "/")
  );
  if (target.origin !== base.origin) {
    return new NextResponse("Bad gateway path", { status: 502 });
  }
  if (isDeniedBridgePath(target.pathname)) {
    // 404, not 403: the browser is told this proxy has no such route, rather
    // than that there is a secret here it may not have.
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }
  target.search = req.nextUrl.search;

  try {
    const headers: Record<string, string> = {
      "Content-Type": req.headers.get("content-type") || "application/json",
      ...(await bridgeAuthHeaders()),
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
    if (contentType.includes("json")) {
      return NextResponse.json(await res.json(), {
        status: res.status,
        headers: { "Cache-Control": res.headers.get("cache-control") || "no-store" },
      });
    }

    // A non-JSON reply is rebuilt rather than streamed, so the headers that
    // decide what the BROWSER does with it have to be carried across by hand.
    // `/api/audit/events.csv` is served as an attachment named
    // `audit-<tenant>-<date>.csv`; without these two the export opened as a
    // wall of text in a tab instead of saving as a file, on a route whose
    // entire purpose is handing an auditor a file.
    //
    // An allowlist, not the whole header set: `Set-Cookie` and the bridge's own
    // auth headers have no business crossing back into the browser.
    const passed = new Headers();
    for (const name of ["content-type", "content-disposition"]) {
      const value = res.headers.get(name);
      if (value) passed.set(name, value);
    }
    // Forwarding the bridge's own Content-Type is what makes the download work;
    // it also means this same-origin path now renders whatever type the bridge
    // names, to a browser that can navigate here directly. Nothing echoes a
    // caller-influenced type today. One header keeps it that way.
    passed.set("x-content-type-options", "nosniff");
    return new NextResponse(await res.text(), { status: res.status, headers: passed });
  } catch {
    return NextResponse.json(
      { error: "Bridge service unavailable" },
      { status: 502 }
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
