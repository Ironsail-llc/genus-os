import { getEngineClient } from "@/lib/engine/server-client";
import { ensureCanvasPromptInjected } from "@/lib/engine/session-state";
import { resolveChatAgent } from "@/lib/chat/agent-guard";

export async function POST(req: Request) {
  const body = await req.json();
  const message = body.message;

  if (!message || typeof message !== "string") {
    return new Response(JSON.stringify({ error: "message required", request_admitted: false }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  // The browser names an AGENT, never a session key. An absent one produces no
  // key — how the engine is told "the main session", exactly as this route has
  // always told it. A NAMED one is checked against the caller's own fleet
  // listing and refused outright if they may not address it; what must never
  // happen is a dropped key falling through to "no key", because "no key" is
  // the operator's shared conversation.
  const chosen = await resolveChatAgent(body.agent);
  if (!chosen.ok) {
    return new Response(JSON.stringify({ error: chosen.error, request_admitted: false }), {
      status: chosen.status,
      headers: { "Content-Type": "application/json" },
    });
  }
  const sessionKey = chosen.key;

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

    // Only a literal `true`: this changes what the request does (offer the text to
    // the running turn; never start one), so nothing merely truthy may turn it on.
    const engineRes = await (body.join_running === true
      ? client.chatSend(message, sessionKey, body.request_id, true)
      : body.request_id === undefined
        ? client.chatSend(message, sessionKey)
        : client.chatSend(message, sessionKey, body.request_id));

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
