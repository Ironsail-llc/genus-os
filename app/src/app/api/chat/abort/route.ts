import { NextResponse } from "next/server";
import { getEngineClient } from "@/lib/engine/server-client";
import { resolveChatTarget } from "@/lib/chat/chat-target";
import { ROOM_ANSWER_NOT_CONFIRMED, echoesRoom } from "@/lib/chat/room-echo";

export async function POST(req: Request) {
  const body = await req.json();
  const chosen = await resolveChatTarget(body.agent, body.room);
  if (!chosen.ok) return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  try {
    const client = getEngineClient();
    if (chosen.roomId) {
      const result = await client.chatAbort("", body.request_id, chosen.roomId);
      if (!echoesRoom(result, chosen.roomId)) {
        return NextResponse.json({ error: ROOM_ANSWER_NOT_CONFIRMED }, { status: 502 });
      }
      return NextResponse.json(result);
    }
    return NextResponse.json(await client.chatAbort(chosen.key, body.request_id));
  } catch {
    return NextResponse.json({ error: "Stop could not be confirmed" }, { status: 502 });
  }
}
