import { getEngineClient } from "@/lib/engine/server-client";
import { ensureCanvasPromptInjected } from "@/lib/engine/session-state";
import { sessionKeyForAgent } from "@/lib/chat/agent-session";

export async function POST(req: Request) {
  const body = await req.json();
  const message = body.message;
  // The browser names an AGENT, never a session key — see `agent-session.ts`.
  // An absent or unusable one produces no key, which is how the engine is told
  // "the main session", exactly as this route has always told it.
  const sessionKey = sessionKeyForAgent(body.agent);

  if (!message || typeof message !== "string") {
    return new Response(JSON.stringify({ error: "message required" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const client = getEngineClient();

  try {
    // Fire-and-forget — cached after first success, no need to block.
    //
    // Main-only, deliberately. The canvas prompt teaches the agent about the
    // dashboard's `[RENDER:…]` markers, and injecting it is a WRITE into
    // whichever session the request names. Pushed into another agent's session
    // it would hand a worker a capability its manifest never granted and a
    // system message its instructions never mention.
    if (!sessionKey) ensureCanvasPromptInjected().catch(() => {});

    const engineRes = await client.chatSend(message, sessionKey);

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
