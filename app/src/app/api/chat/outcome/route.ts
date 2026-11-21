import { NextResponse } from "next/server";
import { getEngineClient } from "@/lib/engine/server-client";
import { resolveChatTarget } from "@/lib/chat/chat-target";
import { ROOM_ANSWER_NOT_CONFIRMED, echoesRoom } from "@/lib/chat/room-echo";

export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const chosen = await resolveChatTarget(params.get("agent"), params.get("room"));
  if (!chosen.ok) return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  const requestId = params.get("request_id");
  if (!requestId) return NextResponse.json({ error: "request_id required" }, { status: 400 });
  try {
    const client = getEngineClient();
    const record = chosen.roomId
      ? await client.chatOutcome(requestId, "", chosen.roomId)
      : await client.chatOutcome(requestId, chosen.key);
    if (chosen.roomId && !echoesRoom(record, chosen.roomId)) {
      return NextResponse.json({ error: ROOM_ANSWER_NOT_CONFIRMED }, { status: 502 });
    }
    return NextResponse.json(record, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json({ error: "Recorded result temporarily unavailable" }, { status: 502 });
  }
}
