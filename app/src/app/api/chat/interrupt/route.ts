import { NextResponse } from "next/server";
import { chatInterrupt } from "@/lib/engine/steer-client";
import { resolveChatAgent } from "@/lib/chat/agent-guard";

/** Stop the live run of the conversation the browser names by agent, at its next step. */
export async function POST(req: Request) {
  const body = await req.json();
  const chosen = await resolveChatAgent(body.agent);
  if (!chosen.ok) return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  try {
    return NextResponse.json(await chatInterrupt(chosen.key));
  } catch {
    return NextResponse.json({ error: "Stop could not be confirmed" }, { status: 502 });
  }
}
