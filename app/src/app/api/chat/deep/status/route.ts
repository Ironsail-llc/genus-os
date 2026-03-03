import { getEngineClient } from "@/lib/engine/server-client";
import { SESSION_KEY } from "@/lib/engine/session-state";

export async function GET() {
  const client = getEngineClient();

  try {
    const result = await client.deepStatus(SESSION_KEY);
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
