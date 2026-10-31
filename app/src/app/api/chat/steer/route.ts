import { NextResponse } from "next/server";
import { chatSteer, chatSteerEnabled } from "@/lib/engine/steer-client";
import { resolveChatAgent } from "@/lib/chat/agent-guard";

/** Whether the composer may steer a running task. Off whenever the engine cannot say. */
export async function GET() {
  try {
    return NextResponse.json(await chatSteerEnabled());
  } catch {
    return NextResponse.json({ enabled: false });
  }
}

/** Add guidance to the live run of the conversation the browser names by agent. */
export async function POST(req: Request) {
  const body = await req.json();
  const text = typeof body.text === "string" ? body.text.trim() : "";
  if (!text) return NextResponse.json({ error: "text required" }, { status: 400 });
  const chosen = await resolveChatAgent(body.agent);
  if (!chosen.ok) return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  const requestId = typeof body.request_id === "string" ? body.request_id : "";
  if (!requestId) return NextResponse.json({ error: "request_id required" }, { status: 400 });
  try {
    return NextResponse.json(await chatSteer(chosen.key, text, requestId));
  } catch {
    return NextResponse.json({ error: "Steer could not be delivered" }, { status: 502 });
  }
}
