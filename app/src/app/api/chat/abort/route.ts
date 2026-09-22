import { NextResponse } from "next/server";
import { getEngineClient } from "@/lib/engine/server-client";
import { resolveChatAgent } from "@/lib/chat/agent-guard";

export async function POST(req: Request) {
  const body = await req.json();
  const chosen = await resolveChatAgent(body.agent);
  if (!chosen.ok) return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  try {
    return NextResponse.json(await getEngineClient().chatAbort(chosen.key, body.request_id));
  } catch {
    return NextResponse.json({ error: "Stop could not be confirmed" }, { status: 502 });
  }
}
