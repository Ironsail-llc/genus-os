/**
 * The chat's SSE reader: one parser for `/chat/send`, `/chat/plan/start` and
 * `/chat/plan/approve`.
 *
 * Each of the three used to carry its own copy of this loop, and the copies had
 * drifted. An event is its `event:` line plus its `data:` lines, closed by a
 * blank line. A network chunk may end anywhere (between those lines, mid-line,
 * inside a multi-byte character), so lines are buffered until whole and the
 * event until its blank line; parsing on `lines[i-1]` once doubled a reply when
 * a chunk split `event: done` from its data.
 *
 * An event whose data is not JSON is skipped, as every copy did. A failed read
 * (an aborted fetch) rejects, so the caller's `catch` still sees it.
 */

/**
 * An event's parsed `data`. The payloads are the engine's and each stream reads
 * the fields it knows, as it did from `JSON.parse` before this reader existed.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type ChatStreamData = Record<string, any>;

export interface ChatStreamEvent {
  /** The `event:` name, or `defaultEvent` when the event had none. */
  event: string;
  data: ChatStreamData;
}

export interface ReadChatStreamOptions {
  /** The name of an event that carried no `event:` line. Default `"delta"`. */
  defaultEvent?: string;
}

export async function* readChatStream(
  res: Response,
  { defaultEvent = "delta" }: ReadChatStreamOptions = {},
): AsyncGenerator<ChatStreamEvent> {
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventType = "";
  let data = "";

  /** One line; returns the event it completes, if any. */
  const take = (line: string): ChatStreamEvent | null => {
    if (line === "") {
      const ready = data ? parse(eventType || defaultEvent, data) : null;
      eventType = "";
      data = "";
      return ready;
    }
    if (line.startsWith("event: ")) eventType = line.slice(7);
    else if (line.startsWith("data: ")) data += (data ? "\n" : "") + line.slice(6);
    return null;
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const event = take(line);
      if (event) yield event;
    }
  }
  buffer += decoder.decode();
  if (buffer) {
    for (const line of buffer.split("\n")) {
      const event = take(line);
      if (event) yield event;
    }
  }
  // An event the stream closed without its blank line.
  const trailing = take("");
  if (trailing) yield trailing;
}

function parse(event: string, data: string): ChatStreamEvent | null {
  try {
    const parsed: unknown = JSON.parse(data);
    if (parsed === null || typeof parsed !== "object") return null;
    return { event, data: parsed as ChatStreamData };
  } catch {
    return null;
  }
}
