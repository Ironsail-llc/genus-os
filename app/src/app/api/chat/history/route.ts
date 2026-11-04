import { NextResponse } from "next/server";
import { getEngineClient } from "@/lib/engine/server-client";
import { ensureCanvasPromptInjected } from "@/lib/engine/session-state";
import { resolveChatAgent } from "@/lib/chat/agent-guard";

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
  const chosen = await resolveChatAgent(url.searchParams.get("agent"));
  if (!chosen.ok) {
    return NextResponse.json({ error: chosen.error }, { status: chosen.status });
  }
  const sessionKey = chosen.key;

  const client = getEngineClient();

  try {
    const result = page
      ? await client.chatHistory(limit, sessionKey, page)
      : await client.chatHistory(limit, sessionKey);

    // Eagerly inject the visual canvas prompt in the background — main only,
    // for the reason `send/route.ts` gives: it is a write into whichever
    // session the request names.
    if (!sessionKey) ensureCanvasPromptInjected().catch(() => {});

    return NextResponse.json({
      messages: result.messages || [],
      sessionKey: result.sessionKey,
      recoveryScope: result.recoveryScope,
      hasMore: result.hasMore === true,
    }, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    return NextResponse.json(
      { error: `Engine error: ${String(err)}`, messages: [] },
      { status: 502 }
    );
  }
}
