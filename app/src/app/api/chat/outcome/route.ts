import { NextResponse } from "next/server";
import { getEngineClient } from "@/lib/engine/server-client";
import { resolveChatAgent } from "@/lib/chat/agent-guard";

export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const chosen = await resolveChatAgent(params.get("agent"));
  if (!chosen.ok) return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  const requestId = params.get("request_id");
  if (!requestId) return NextResponse.json({ error: "request_id required" }, { status: 400 });
  try {
    return NextResponse.json(await getEngineClient().chatOutcome(requestId, chosen.key), {
      headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json({ error: "Recorded result temporarily unavailable" }, { status: 502 });
  }
}
