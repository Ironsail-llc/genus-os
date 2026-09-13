import { getEngineClient } from "@/lib/engine/server-client";

export async function GET() {
  const client = getEngineClient();

  try {
    const result = await client.deepStatus();
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
