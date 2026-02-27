import { NextResponse } from "next/server";
import { getEngineClient } from "@/lib/gateway/server-client";
import { ensureCanvasPromptInjected, SESSION_KEY } from "@/lib/gateway/session-state";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const limit = parseInt(url.searchParams.get("limit") || "50", 10);

  const client = getEngineClient();

  try {
    const result = await client.chatHistory(SESSION_KEY, limit);

    // Eagerly inject visual canvas prompt in background while returning history
    ensureCanvasPromptInjected().catch(() => {});

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
