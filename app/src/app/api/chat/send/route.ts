import { getEngineClient } from "@/lib/engine/server-client";
import { ensureCanvasPromptInjected } from "@/lib/engine/session-state";
import { resolveChatAgent } from "@/lib/chat/agent-guard";
import { MAX_SEND_BODY_BYTES, checkAttachments, type AttachmentPayload } from "@/lib/chat/attachments";
import { EngineRefusal, REFUSAL_STATUSES } from "@/lib/engine/engine-refusal";

function json(status: number, body: Record<string, unknown>): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/**
 * The request's JSON, or the refusal to answer instead. A body shorter than
 * its Content-Length arrived truncated (a proxy that buffered only part of
 * it): 413. One that does not parse: 400. Either way no turn starts, and the
 * browser is told so rather than handed a bare 500 and a recovery card.
 */
/** What the browser may send. Shapes, not guarantees: every field is checked below. */
interface SendBody {
  message?: string;
  request_id?: string;
  agent?: string;
  join_running?: boolean;
  attachments?: AttachmentPayload[];
}

async function readBody(req: Request): Promise<SendBody | Response> {
  let raw: string;
  try {
    raw = await req.text();
  } catch {
    return json(400, { error: "the request body could not be read", request_admitted: false });
  }
  const declared = Number(req.headers.get("content-length") ?? NaN);
  if (Number.isFinite(declared) && new TextEncoder().encode(raw).length < declared) {
    return json(413, { error: "the message and its files arrived incomplete; they are too large", request_admitted: false });
  }
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as SendBody;
  } catch {
    // fall through to the refusal
  }
  return json(400, { error: "the request body is not valid JSON", request_admitted: false });
}

export async function POST(req: Request) {
  // Refused before it is read: three files at the limit, base64'd, fit well
  // inside this, so a larger body can only be one the checks below would refuse.
  if (Number(req.headers.get("content-length") ?? 0) > MAX_SEND_BODY_BYTES) {
    return json(413, { error: "the message and its files are too large", request_admitted: false });
  }
  const body = await readBody(req);
  if (body instanceof Response) return body;
  const message = body.message;
  const attachments: AttachmentPayload[] | undefined = body.attachments;
  const hasFiles = Array.isArray(attachments) && attachments.length > 0;

  if (typeof message !== "string" || (!message && !hasFiles)) {
    return json(400, { error: "message required", request_admitted: false });
  }

  // Files ride with the turn. Checked here against the composer's own limits
  // (413 too many or too large, 422 a type off the list or bytes that are not
  // base64) and again by the engine, which keeps them.
  const refused = hasFiles && body.join_running === true
    ? { status: 422, error: "files cannot be added to a running task; send them next" }
    : checkAttachments(attachments);
  if (refused) {
    return json(refused.status, { error: refused.error, request_admitted: false });
  }

  // The browser names an AGENT, never a session key. An absent one produces no
  // key — how the engine is told "the main session", exactly as this route has
  // always told it. A NAMED one is checked against the caller's own fleet
  // listing and refused outright if they may not address it; what must never
  // happen is a dropped key falling through to "no key", because "no key" is
  // the operator's shared conversation.
  const chosen = await resolveChatAgent(body.agent);
  if (!chosen.ok) {
    return json(chosen.status, { error: chosen.error, request_admitted: false });
  }
  const sessionKey = chosen.key;

  const client = getEngineClient();

  try {
    // Fire-and-forget — cached after first success, no need to block.
    //
    // Main-only, deliberately. The canvas prompt teaches the agent about the
    // dashboard's `[RENDER:…]` markers, and injecting it is a WRITE into
    // whichever session the request names. Pushed into another agent's session
    // it would hand a worker a capability its manifest never granted and a
    // system message its instructions never mention.
    if (!sessionKey) ensureCanvasPromptInjected().catch(() => {});

    // Only a literal `true`: this changes what the request does (offer the text to
    // the running turn; never start one), so nothing merely truthy may turn it on.
    const engineRes = await (body.join_running === true
      ? client.chatSend(message, sessionKey, body.request_id, true)
      : hasFiles
        ? client.chatSend(message, sessionKey, body.request_id, false, attachments)
        : body.request_id === undefined
        ? client.chatSend(message, sessionKey)
        : client.chatSend(message, sessionKey, body.request_id));

    if (!engineRes.body) {
      return json(502, { error: "No response body from engine" });
    }

    // Pipe engine SSE directly to browser — marker interception is client-side
    return new Response(engineRes.body, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      },
    });
  } catch (err) {
    if (err instanceof EngineRefusal) {
      // A refusal keeps its status; an engine failure before admission is
      // still this route's 502, but with the engine's words and no turn.
      const status = REFUSAL_STATUSES.has(err.status) ? err.status : 502;
      return json(status, { error: err.message, request_admitted: false });
    }
    return json(502, { error: `Engine error: ${String(err)}` });
  }
}
