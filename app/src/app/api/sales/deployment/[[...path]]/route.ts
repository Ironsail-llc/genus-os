import { bridgeAuthHeaders } from "@/lib/bridge-auth";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
const ENGINE_URL = process.env.ROBOTHOR_ENGINE_URL || "http://127.0.0.1:18800";
const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";
type Context = { params: Promise<{ path?: string[] }> };

async function proxy(request: Request, context: Context) {
  const path = (await context.params).path?.join("/") ?? "";
  const allowed = request.method === "GET"
    ? path === "" || /^releases\/[a-f0-9]{64}$/.test(path)
    : path === "prepare" || new RegExp(`^transitions/${UUID}/(commit|abort|rollback)$`).test(path);
  if (!allowed) return Response.json({ error: "Not found" }, { status: 404 });
  const identity = await bridgeAuthHeaders();
  if (!identity.Authorization?.startsWith("Bearer ")) {
    return Response.json({ error: "Authenticated session required" }, { status: 401 });
  }
  if (request.method === "POST" && !request.headers.get("content-type")?.startsWith("application/json")) {
    return Response.json({ error: "JSON body required" }, { status: 415 });
  }
  const body = request.method === "POST" ? await request.text() : undefined;
  if (body && new TextEncoder().encode(body).length > 16_384) {
    return Response.json({ error: "Request too large" }, { status: 413 });
  }
  try {
    const response = await fetch(`${ENGINE_URL.replace(/\/$/, "")}/api/admin/sales-deployment${path ? `/${path}` : ""}`, {
      method: request.method,
      headers: { Authorization: identity.Authorization, "Content-Type": "application/json" },
      body, redirect: "error", cache: "no-store", signal: AbortSignal.timeout(120_000),
    });
    return Response.json(await response.json(), { status: response.status, headers: { "Cache-Control": "no-store" } });
  } catch {
    return Response.json({ error: "Engine response unavailable; refresh deployment status before retrying" }, { status: 502 });
  }
}
export const GET = proxy;
export const POST = proxy;
