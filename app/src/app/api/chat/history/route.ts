import { NextResponse } from "next/server";
import { getEngineClient } from "@/lib/engine/server-client";
import { ensureCanvasPromptInjected } from "@/lib/engine/session-state";
import { sessionKeyForAgent } from "@/lib/chat/agent-session";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const limit = parseInt(url.searchParams.get("limit") || "50", 10);
  const sessionKey = sessionKeyForAgent(url.searchParams.get("agent"));

  const client = getEngineClient();

  try {
    const result = await client.chatHistory(limit, sessionKey);

    // Eagerly inject the visual canvas prompt in the background — main only,
    // for the reason `send/route.ts` gives: it is a write into whichever
    // session the request names.
    if (!sessionKey) ensureCanvasPromptInjected().catch(() => {});

    return NextResponse.json({
      messages: result.messages || [],
      sessionKey: result.sessionKey,
    });
  } catch (err) {
    return NextResponse.json(
      { error: `Engine error: ${String(err)}`, messages: [] },
      { status: 502 }
    );
  }
}
