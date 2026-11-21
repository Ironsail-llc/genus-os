import { NextResponse } from "next/server";
import { getEngineClient } from "@/lib/engine/server-client";
import { resolveChatTarget } from "@/lib/chat/chat-target";

export async function POST(req: Request) {
  const body = await req.json();
  const chosen = await resolveChatTarget(body.agent, body.room);
  if (!chosen.ok) return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  try {
    const client = getEngineClient();
    return NextResponse.json(
      await (chosen.roomId
        ? client.chatAbort("", body.request_id, chosen.roomId)
        : client.chatAbort(chosen.key, body.request_id)),
    );
  } catch {
    return NextResponse.json({ error: "Stop could not be confirmed" }, { status: 502 });
  }
}
