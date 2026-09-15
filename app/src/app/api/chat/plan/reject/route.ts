import { getEngineClient } from "@/lib/engine/server-client";
import { resolveChatAgent } from "@/lib/chat/agent-guard";

export async function POST(req: Request) {
  const body = await req.json();
  const planId = body.plan_id;
  const feedback = body.feedback;
  const chosen = await resolveChatAgent(body.agent);
  if (!chosen.ok) {
      return new Response(JSON.stringify({ error: chosen.error }), {
        status: chosen.status,
        headers: { "Content-Type": "application/json" },
      });
  }
  const sessionKey = chosen.key;

  if (!planId || typeof planId !== "string") {
    return new Response(JSON.stringify({ error: "plan_id required" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const client = getEngineClient();

  try {
    const result = await client.planReject(planId, feedback, sessionKey);
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
