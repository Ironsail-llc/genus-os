import { NextResponse } from "next/server";
import { chatInterrupt } from "@/lib/engine/steer-client";
import { resolveChatAgent } from "@/lib/chat/agent-guard";

/** Stop the live run of this tab's turn (agent + request id), as the runs API interrupt does. */
export async function POST(req: Request) {
  const body = await req.json();
  const chosen = await resolveChatAgent(body.agent);
  if (!chosen.ok) return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  const requestId = typeof body.request_id === "string" ? body.request_id : "";
  if (!requestId) return NextResponse.json({ error: "request_id required" }, { status: 400 });
  try {
    return NextResponse.json(await chatInterrupt(chosen.key, requestId));
  } catch {
    return NextResponse.json({ error: "Stop could not be confirmed" }, { status: 502 });
  }
}
