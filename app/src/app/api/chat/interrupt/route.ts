import { NextResponse } from "next/server";
import { chatInterrupt } from "@/lib/engine/steer-client";
import { resolveChatTarget } from "@/lib/chat/chat-target";
import { ROOM_ANSWER_NOT_CONFIRMED, echoesRoom } from "@/lib/chat/room-echo";

/** Stop the live run of this tab's turn (agent + request id), as the runs API interrupt does. */
export async function POST(req: Request) {
  const body = await req.json();
  const chosen = await resolveChatTarget(body.agent, body.room);
  if (!chosen.ok) return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  const requestId = typeof body.request_id === "string" ? body.request_id : "";
  if (!requestId) return NextResponse.json({ error: "request_id required" }, { status: 400 });
  try {
    if (chosen.roomId) {
      const result = await chatInterrupt("", requestId, chosen.roomId);
      if (!echoesRoom(result, chosen.roomId)) {
        return NextResponse.json({ error: ROOM_ANSWER_NOT_CONFIRMED }, { status: 502 });
      }
      return NextResponse.json(result);
    }
    return NextResponse.json(await chatInterrupt(chosen.key, requestId));
  } catch {
    return NextResponse.json({ error: "Stop could not be confirmed" }, { status: 502 });
  }
}
