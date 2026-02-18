import { NextResponse } from "next/server";
import { getGatewayClient } from "@/lib/gateway/server-client";

const SESSION_KEY = "agent:main:webchat-philip";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const limit = parseInt(url.searchParams.get("limit") || "50", 10);

  const client = getGatewayClient();

  try {
    await client.ensureConnected();
    const result = await client.chatHistory(SESSION_KEY, limit);
    return NextResponse.json({
      messages: result.messages || [],
      sessionKey: result.sessionKey,
    });
  } catch (err) {
    return NextResponse.json(
      { error: `Gateway error: ${String(err)}`, messages: [] },
      { status: 502 }
    );
  }
}
