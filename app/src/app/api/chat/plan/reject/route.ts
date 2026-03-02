import { getEngineClient } from "@/lib/engine/server-client";
import { SESSION_KEY } from "@/lib/engine/session-state";

export async function POST(req: Request) {
  const body = await req.json();
  const planId = body.plan_id;
  const feedback = body.feedback;

  if (!planId || typeof planId !== "string") {
    return new Response(JSON.stringify({ error: "plan_id required" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const client = getEngineClient();

  try {
    const result = await client.planReject(SESSION_KEY, planId, feedback);
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
