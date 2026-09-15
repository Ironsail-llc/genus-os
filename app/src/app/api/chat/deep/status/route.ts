import { getEngineClient } from "@/lib/engine/server-client";
import { sessionKeyForAgent } from "@/lib/chat/agent-session";

export async function GET(req: Request) {
  const sessionKey = sessionKeyForAgent(new URL(req.url).searchParams.get("agent"));
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
