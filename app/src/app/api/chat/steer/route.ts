import { NextResponse } from "next/server";
import { chatSteer } from "@/lib/engine/steer-client";
import { resolveChatTarget } from "@/lib/chat/chat-target";
import { getEngineClient } from "@/lib/engine/server-client";
import { ROOM_ANSWER_NOT_CONFIRMED, ROOM_NOT_CONFIRMED, confirmRoom, echoesRoom } from "@/lib/chat/room-echo";

/** Add guidance to the live run of the conversation the browser names by agent or room. */
export async function POST(req: Request) {
  const body = await req.json();
  const text = typeof body.text === "string" ? body.text.trim() : "";
  if (!text) return NextResponse.json({ error: "text required" }, { status: 400 });
  const chosen = await resolveChatTarget(body.agent, body.room);
  if (!chosen.ok) return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  const requestId = typeof body.request_id === "string" ? body.request_id : "";
  if (!requestId) return NextResponse.json({ error: "request_id required" }, { status: 400 });
  try {
    const roomId = chosen.roomId;
    if (roomId) {
      // Steering posts text: confirmed first, like a send, so it never reaches
      // an engine that would read the request as the main conversation's.
      if (!(await confirmRoom(roomId, () => getEngineClient().chatHistory(1, "", {}, roomId)))) {
        return NextResponse.json({ error: ROOM_NOT_CONFIRMED }, { status: 502 });
      }
      const result = await chatSteer("", text, requestId, roomId);
      if (!echoesRoom(result, roomId)) {
        return NextResponse.json({ error: ROOM_ANSWER_NOT_CONFIRMED }, { status: 502 });
      }
      return NextResponse.json(result);
    }
    return NextResponse.json(await chatSteer(chosen.key, text, requestId));
  } catch {
    return NextResponse.json({ error: "Steer could not be delivered" }, { status: 502 });
  }
}
