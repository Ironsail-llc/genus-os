import { getGatewayClient } from "@/lib/gateway/server-client";
import { extractText } from "@/lib/gateway/types";
import type { ChatEvent } from "@/lib/gateway/types";
import { getVisualCanvasPrompt } from "@/lib/system-prompt";
import { MarkerInterceptor } from "@/lib/gateway/marker-interceptor";

const SESSION_KEY = "agent:main:webchat-philip";

/** Track whether we've injected the visual canvas prompt this process */
let injectedSession = false;

async function ensureSessionInjected() {
  if (injectedSession) return;
  const client = getGatewayClient();
  try {
    await client.chatInject(
      SESSION_KEY,
      getVisualCanvasPrompt(),
      "visual-canvas-init"
    );
    injectedSession = true;
  } catch {
    // Session may not exist yet — injection will happen on first history call
    console.warn("[chat/send] Could not inject visual canvas prompt");
  }
}

export async function POST(req: Request) {
  const body = await req.json();
  const message = body.message;

  if (!message || typeof message !== "string") {
    return new Response(JSON.stringify({ error: "message required" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const client = getGatewayClient();

  try {
    await client.ensureConnected();
    await ensureSessionInjected();

    const { events } = await client.chatSend(SESSION_KEY, message);

    const encoder = new TextEncoder();
    const interceptor = new MarkerInterceptor();
    let fullCleanText = "";
    let previousCumulative = ""; // Gateway sends cumulative text — track for delta extraction

    const stream = new ReadableStream({
      async start(controller) {
        try {
          for await (const event of events) {
            const cumulativeText = extractEventText(event);

            if (event.state === "delta" && cumulativeText) {
              // Gateway sends cumulative text (full response so far) — extract only the new delta
              const deltaText = cumulativeText.substring(previousCumulative.length);
              previousCumulative = cumulativeText;

              if (!deltaText) continue;

              // Run DELTA text through marker interceptor
              const result = interceptor.addChunk(deltaText);

              // Emit clean text delta
              if (result.text) {
                fullCleanText += result.text;
                controller.enqueue(
                  encoder.encode(
                    `event: delta\ndata: ${JSON.stringify({ text: result.text })}\n\n`
                  )
                );
              }

              // Emit any extracted markers as separate events
              for (const marker of result.markers) {
                controller.enqueue(
                  encoder.encode(
                    `event: ${marker.type}\ndata: ${JSON.stringify(marker)}\n\n`
                  )
                );
              }
            } else if (event.state === "final") {
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
                  `event: done\ndata: ${JSON.stringify({ text: fullCleanText })}\n\n`
                )
              );
            } else if (event.state === "error") {
              controller.enqueue(
                encoder.encode(
                  `event: error\ndata: ${JSON.stringify({ error: event.errorMessage || "Unknown error" })}\n\n`
                )
              );
            } else if (event.state === "aborted") {
              const flushed = interceptor.flush();
              if (flushed.text) fullCleanText += flushed.text;
              controller.enqueue(
                encoder.encode(
                  `event: done\ndata: ${JSON.stringify({ text: fullCleanText, aborted: true })}\n\n`
                )
              );
            }
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
      JSON.stringify({ error: `Gateway error: ${String(err)}` }),
      { status: 502, headers: { "Content-Type": "application/json" } }
    );
  }
}

function extractEventText(event: ChatEvent): string {
  if (!event.message) return "";
  return extractText(event.message);
}
