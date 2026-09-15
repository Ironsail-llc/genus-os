import { getEngineClient } from "@/lib/engine/server-client";
import { resolveChatAgent } from "@/lib/chat/agent-guard";

export async function GET(req: Request) {
  const chosen = await resolveChatAgent(new URL(req.url).searchParams.get("agent"));
  if (!chosen.ok) {
      return new Response(JSON.stringify({ error: chosen.error }), {
        status: chosen.status,
        headers: { "Content-Type": "application/json" },
      });
  }
  const sessionKey = chosen.key;
  const client = getEngineClient();

  try {
    const result = await client.deepStatus(sessionKey);
    return new Response(JSON.stringify(result), {
      headers: { "Content-Type": "application/json" },
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: `Engine error: ${String(err)}` }),
      { status: 502, headers: { "Content-Type": "application/json" } }
    );
  }
}
