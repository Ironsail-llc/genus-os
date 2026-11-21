import { NextResponse } from "next/server";
import { getEngineClient } from "@/lib/engine/server-client";
import { ensureCanvasPromptInjected } from "@/lib/engine/session-state";
import { resolveChatTarget } from "@/lib/chat/chat-target";
import { ROOM_ANSWER_NOT_CONFIRMED, echoesRoom } from "@/lib/chat/room-echo";
import { EngineRefusal } from "@/lib/engine/engine-refusal";

function wholeNumber(value: string | null): number | undefined {
  return value && /^\d+$/.test(value) && Number(value) > 0 ? Number(value) : undefined;
}

function historyPage(params: URLSearchParams): { before?: number; offset?: number } | undefined {
  const before = wholeNumber(params.get("before"));
  const offset = wholeNumber(params.get("offset"));
  if (before === undefined && offset === undefined) return undefined;
  return { ...(before !== undefined ? { before } : {}), ...(offset !== undefined ? { offset } : {}) };
}

export async function GET(req: Request) {
  const url = new URL(req.url);
  const limit = parseInt(url.searchParams.get("limit") || "50", 10);
  // Paging past the first page: a stored-row cursor or an offset, each a
  // positive whole number or not sent at all.
  const page = historyPage(url.searchParams);

  // Checked server-side, like every other route here: reading another agent's
  // conversation is as much a capability as writing to it.
  // A room is named by id and only its shape is checked here; the engine
  // decides whether this caller may read it.
  const chosen = await resolveChatTarget(url.searchParams.get("agent"), url.searchParams.get("room"));
  if (!chosen.ok) {
    return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  }
  const sessionKey = chosen.key;
  const roomId = chosen.roomId;

  const client = getEngineClient();

  try {
    const result = roomId
      ? await client.chatHistory(limit, "", page ?? {}, roomId)
      : page
        ? await client.chatHistory(limit, sessionKey, page)
        : await client.chatHistory(limit, sessionKey);

    // Eagerly inject the visual canvas prompt in the background — main only,
    // for the reason `send/route.ts` gives: it is a write into whichever
    // session the request names.
    if (!sessionKey && !roomId) ensureCanvasPromptInjected().catch(() => {});

    if (roomId) {
      // An engine that ignored `room_id` answered with the caller's MAIN
      // conversation: none of it is shown. And a room's session key is the
      // engine's business, never the browser's.
      if (!echoesRoom(result, roomId)) {
        return NextResponse.json({ error: ROOM_ANSWER_NOT_CONFIRMED, messages: [] }, { status: 502 });
      }
      return NextResponse.json({
        messages: result.messages || [],
        recoveryScope: result.recoveryScope,
        hasMore: result.hasMore === true,
      }, { headers: { "Cache-Control": "no-store" } });
    }

    return NextResponse.json({
      messages: result.messages || [],
      sessionKey: result.sessionKey,
      recoveryScope: result.recoveryScope,
      hasMore: result.hasMore === true,
    }, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    if (roomId && err instanceof EngineRefusal) {
      // No access to the workspace (404) or not allowed (403): said as such.
      return NextResponse.json({ error: err.message, messages: [] }, { status: err.status });
    }
    return NextResponse.json(
      { error: `Engine error: ${String(err)}`, messages: [] },
      { status: 502 }
    );
  }
}
