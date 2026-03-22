import { getEngineClient } from "@/lib/engine/server-client";
import { ensureCanvasPromptInjected, SESSION_KEY } from "@/lib/engine/session-state";

export async function POST(req: Request) {
  const body = await req.json();
  const message = body.message;

  if (!message || typeof message !== "string") {
    return new Response(JSON.stringify({ error: "message required" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const client = getEngineClient();

  try {
    // Fire-and-forget — cached after first success, no need to block
    ensureCanvasPromptInjected().catch(() => {});

    const engineRes = await client.chatSend(SESSION_KEY, message);

    if (!engineRes.body) {
      return new Response(
        JSON.stringify({ error: "No response body from engine" }),
        { status: 502, headers: { "Content-Type": "application/json" } }
      );
    }

    // Pipe engine SSE directly to browser — marker interception is client-side
    return new Response(engineRes.body, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      },
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: `Engine error: ${String(err)}` }),
      { status: 502, headers: { "Content-Type": "application/json" } }
    );
  }
}
