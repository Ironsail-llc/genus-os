import { getEngineClient } from "@/lib/gateway/server-client";
import { ensureCanvasPromptInjected, SESSION_KEY } from "@/lib/gateway/session-state";
import { MarkerInterceptor } from "@/lib/gateway/marker-interceptor";

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
    await ensureCanvasPromptInjected();

    const engineRes = await client.chatSend(SESSION_KEY, message);

    // 409 = session busy
    if (engineRes.status === 409) {
      return new Response(
        JSON.stringify({ error: "Session is busy" }),
        { status: 409, headers: { "Content-Type": "application/json" } }
      );
    }

    if (!engineRes.body) {
      return new Response(
        JSON.stringify({ error: "No response body from engine" }),
        { status: 502, headers: { "Content-Type": "application/json" } }
      );
    }

    const encoder = new TextEncoder();
    const interceptor = new MarkerInterceptor();
    let fullCleanText = "";

    const stream = new ReadableStream({
      async start(controller) {
        let sentDone = false;
        try {
          const reader = engineRes.body!.getReader();
          const decoder = new TextDecoder();
          let buffer = "";

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            // Parse SSE events from buffer
            const lines = buffer.split("\n");
            buffer = lines.pop() || ""; // Keep incomplete line in buffer

            let eventType = "";
            for (const line of lines) {
              if (line.startsWith("event: ")) {
                eventType = line.slice(7).trim();
              } else if (line.startsWith("data: ")) {
                const dataStr = line.slice(6);
                let data: Record<string, unknown>;
                try {
                  data = JSON.parse(dataStr);
                } catch {
                  continue;
                }

                if (eventType === "delta" && data.text) {
                  const deltaText = data.text as string;

                  // Run delta through marker interceptor
                  const result = interceptor.addChunk(deltaText);

                  if (result.text) {
                    fullCleanText += result.text;
                    controller.enqueue(
                      encoder.encode(
                        `event: delta\ndata: ${JSON.stringify({ text: result.text })}\n\n`
                      )
                    );
                  }

                  for (const marker of result.markers) {
                    controller.enqueue(
                      encoder.encode(
                        `event: ${marker.type}\ndata: ${JSON.stringify(marker)}\n\n`
                      )
                    );
                  }
                } else if (eventType === "done") {
                  // Flush any buffered text/markers
                  const flushed = interceptor.flush();
                  if (flushed.text) {
                    fullCleanText += flushed.text;
                    controller.enqueue(
                      encoder.encode(
                        `event: delta\ndata: ${JSON.stringify({ text: flushed.text })}\n\n`
                      )
                    );
                  }
                  for (const marker of flushed.markers) {
                    controller.enqueue(
                      encoder.encode(
                        `event: ${marker.type}\ndata: ${JSON.stringify(marker)}\n\n`
                      )
                    );
                  }

                  controller.enqueue(
                    encoder.encode(
                      `event: done\ndata: ${JSON.stringify({ text: fullCleanText, ...((data.aborted) ? { aborted: true } : {}) })}\n\n`
                    )
                  );
                  sentDone = true;
                } else if (eventType === "error") {
                  controller.enqueue(
                    encoder.encode(
                      `event: error\ndata: ${JSON.stringify({ error: data.error || "Unknown error" })}\n\n`
                    )
                  );
                }

                eventType = "";
              }
            }
          }
        } catch (err) {
          controller.enqueue(
            encoder.encode(
              `event: error\ndata: ${JSON.stringify({ error: String(err) })}\n\n`
            )
          );
        } finally {
          if (!sentDone) {
            const flushed = interceptor.flush();
            if (flushed.text) {
              fullCleanText += flushed.text;
              controller.enqueue(
                encoder.encode(
                  `event: delta\ndata: ${JSON.stringify({ text: flushed.text })}\n\n`
                )
              );
            }
            controller.enqueue(
              encoder.encode(
                `event: done\ndata: ${JSON.stringify({ text: fullCleanText })}\n\n`
              )
            );
          }
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
