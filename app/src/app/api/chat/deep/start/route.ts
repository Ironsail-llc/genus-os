import { getEngineClient } from "@/lib/engine/server-client";
import { SESSION_KEY } from "@/lib/engine/session-state";

export async function POST(req: Request) {
  const body = await req.json();
  const query = body.query || body.message;

  if (!query || typeof query !== "string") {
    return new Response(JSON.stringify({ error: "query required" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const client = getEngineClient();

  try {
    const engineRes = await client.deepStart(SESSION_KEY, query);

    if (!engineRes.body) {
      return new Response(
        JSON.stringify({ error: "No response body from engine" }),
        { status: 502, headers: { "Content-Type": "application/json" } }
      );
    }

    // Proxy the SSE stream directly
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      async start(controller) {
        try {
          const reader = engineRes.body!.getReader();
          const decoder = new TextDecoder();
          let buffer = "";

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";

            for (const line of lines) {
              controller.enqueue(encoder.encode(line + "\n"));
            }
          }
          if (buffer) {
            controller.enqueue(encoder.encode(buffer));
          }
        } catch (err) {
          controller.enqueue(
            encoder.encode(
              `event: error\ndata: ${JSON.stringify({ error: String(err) })}\n\n`
            )
          );
        } finally {
          controller.close();
        }
      },
    });

    return new Response(stream, {
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
