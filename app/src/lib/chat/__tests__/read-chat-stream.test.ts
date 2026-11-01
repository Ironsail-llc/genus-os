// @vitest-environment node
/**
 * One reader for the chat's three SSE streams.
 *
 * `/chat/send`, `/chat/plan/start` and `/chat/plan/approve` each carried a
 * copy of the same line parser. These fixtures are the shapes those copies
 * handle today: an event is its `event:` line plus its `data:` lines, closed
 * by a blank line, and a TCP chunk may end anywhere — between the two lines,
 * mid-line, or inside a multi-byte character.
 */
import { describe, it, expect } from "vitest";
import { readChatStream, type ChatStreamEvent } from "../read-chat-stream";

function responseOf(chunks: Array<string | Uint8Array>): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
        controller.close();
      },
    }),
  );
}

async function collect(res: Response, options?: { defaultEvent?: string }): Promise<ChatStreamEvent[]> {
  const out: ChatStreamEvent[] = [];
  for await (const event of readChatStream(res, options)) out.push(event);
  return out;
}

const sse = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

describe("readChatStream", () => {
  it("yields every event the three call sites dispatch, in order", async () => {
    const fixtures: Array<[string, Record<string, unknown>]> = [
      ["accepted", { text: "Accepted" }],
      ["progress", { text: "Working", elapsed_s: 3 }],
      ["approval_required", { id: "q1", kind: "question", question: "Which?", options: ["a", "b"] }],
      ["tool_start", { tool: "search_memory", call_id: "c1" }],
      ["tool_end", { tool: "search_memory", call_id: "c1" }],
      ["iteration_start", { iteration: 2, max_iterations: 5 }],
      ["delta", { text: "Hello " }],
      ["dashboard", { data: { tasks: 3 } }],
      ["render", { component: "render_table", props: { rows: [] } }],
      ["interim", { text: "Earlier answer" }],
      ["pending_followups", { messages: ["and also"] }],
      ["plan", { plan_id: "p1", plan_text: "1. Do it", original_message: "do", status: "pending" }],
      ["deep_progress", { elapsed_s: 12 }],
      ["deep_result", { response: "Deep answer", execution_time_s: 4.2, cost_usd: 0.31 }],
      ["error", { error: "boom" }],
      ["done", { status: "completed", text: "Hello world" }],
    ];
    const res = responseOf([fixtures.map(([event, data]) => sse(event, data)).join("")]);
    expect(await collect(res)).toEqual(fixtures.map(([event, data]) => ({ event, data })));
  });

  it("joins an event split between its event and data lines across chunks", async () => {
    const res = responseOf(["event: done\n", 'data: {"text":"whole"}\n', "\n"]);
    expect(await collect(res)).toEqual([{ event: "done", data: { text: "whole" } }]);
  });

  it("joins a line split mid-way across chunks", async () => {
    const res = responseOf(["event: del", 'ta\ndata: {"te', 'xt":"hi"}\n\n']);
    expect(await collect(res)).toEqual([{ event: "delta", data: { text: "hi" } }]);
  });

  it("decodes a multi-byte character split across chunks", async () => {
    const bytes = new TextEncoder().encode(sse("delta", { text: "café ✓" }));
    const cut = bytes.indexOf(0xc3) + 1; // inside the two-byte "é"
    const res = responseOf([bytes.slice(0, cut), bytes.slice(cut)]);
    expect(await collect(res)).toEqual([{ event: "delta", data: { text: "café ✓" } }]);
  });

  it("joins multiple data lines with a newline", async () => {
    const res = responseOf(['event: plan\ndata: {"plan_text":\ndata: "two lines"}\n\n']);
    expect(await collect(res)).toEqual([{ event: "plan", data: { plan_text: "two lines" } }]);
  });

  it("names an event with no event line delta, unless told otherwise", async () => {
    const body = 'data: {"text":"bare"}\n\n';
    expect(await collect(responseOf([body]))).toEqual([{ event: "delta", data: { text: "bare" } }]);
    // The plan-approve stream never defaulted a bare event to delta.
    expect(await collect(responseOf([body]), { defaultEvent: "" })).toEqual([{ event: "", data: { text: "bare" } }]);
  });

  it("dispatches a trailing event the stream closed without a blank line", async () => {
    const res = responseOf([sse("delta", { text: "a" }), 'event: done\ndata: {"text":"end"}']);
    expect(await collect(res)).toEqual([
      { event: "delta", data: { text: "a" } },
      { event: "done", data: { text: "end" } },
    ]);
  });

  it("skips an event whose data is not JSON and carries on", async () => {
    const res = responseOf(["event: delta\ndata: {not json\n\n", sse("done", { text: "ok" })]);
    expect(await collect(res)).toEqual([{ event: "done", data: { text: "ok" } }]);
  });

  it("dispatches nothing for an event line with no data, and resets the type after", async () => {
    const res = responseOf(["event: ping\n\n", 'data: {"text":"x"}\n\n']);
    expect(await collect(res)).toEqual([{ event: "delta", data: { text: "x" } }]);
  });

  it("ignores comment and unknown field lines", async () => {
    const res = responseOf([": keepalive\nid: 7\nevent: delta\ndata: {\"text\":\"y\"}\n\n"]);
    expect(await collect(res)).toEqual([{ event: "delta", data: { text: "y" } }]);
  });

  it("yields nothing for an empty stream", async () => {
    expect(await collect(responseOf([]))).toEqual([]);
  });

  it("rejects when the read fails, so an aborted fetch reaches the caller's catch", async () => {
    const res = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(sse("delta", { text: "a" })));
          controller.error(Object.assign(new Error("aborted"), { name: "AbortError" }));
        },
      }),
    );
    await expect(collect(res)).rejects.toMatchObject({ name: "AbortError" });
  });
});
