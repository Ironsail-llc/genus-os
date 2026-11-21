/**
 * The dashboard's chat routes, through the real engine client, against what
 * the engine REALLY answers a workspace.
 *
 * `contract/engine-replies.json` is recorded from the real chat and periods
 * routes over Postgres (`robothor/engine/tests/test_rooms_engine_contract.py`,
 * which fails when a reply changes). Only `fetch` (answered from the
 * recording) and the caller's bridge credentials are stand-ins here: the
 * Next.js route, `resolveChatTarget`, the room echo check and the
 * `EngineClient` all run as they do in production.
 *
 * The seam: `room_id` goes in and no session key; the first SSE event is
 * `room` `{roomId}`; a busy room is 409 naming who is answered; a workspace
 * the caller cannot see is 404 JSON (never "the engine broke"); a viewer's
 * send is 403; and the engine's edition is the bridge's edition.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/bridge-auth", () => ({ bridgeAuthHeaders: async () => ({ Authorization: "Bearer caller-token" }) }));
vi.mock("@/lib/engine/session-state", () => ({ ensureCanvasPromptInjected: async () => undefined }));

import engineRecorded from "./contract/engine-replies.json";
import bridgeRecorded from "./contract/bridge-replies.json";
import { historyToMessages } from "@/lib/chat/history-messages";
import { ROOM_NOT_CONFIRMED } from "@/lib/chat/room-echo";

type Recorded = { status: number; body: unknown };

/** The recording names the room `room-report`; the routes need a room id's shape. */
const ROOM = "7e1d6f3a-2b4c-4d5e-8f60-718293a4b5c6";
const engine = JSON.parse(JSON.stringify(engineRecorded).replaceAll("room-report", ROOM)) as Record<string, Recorded>;
const bridge = bridgeRecorded as Record<string, Recorded>;

function reply(name: string): Response {
  const r = engine[name];
  if (!r) throw new Error(`no recorded reply ${name}`);
  if (typeof r.body === "string") {
    return new Response(r.body, { status: r.status, headers: { "Content-Type": "text/event-stream" } });
  }
  return new Response(JSON.stringify(r.body), { status: r.status, headers: { "Content-Type": "application/json" } });
}

/** Answer engine requests by path: `/chat/history` and `/chat/send` each from a named recording. */
function engineAnswers(answers: { history?: string; send?: string }) {
  const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async (input) => {
    const url = String(input);
    if (url.includes("/chat/history") && answers.history) return reply(answers.history);
    if (url.includes("/chat/send") && answers.send) return reply(answers.send);
    throw new Error(`unexpected engine call ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function routes() {
  vi.resetModules();
  return {
    send: (await import("@/app/api/chat/send/route")).POST,
    history: (await import("@/app/api/chat/history/route")).GET,
  };
}

function sendRequest(message: string) {
  return new Request("http://helm.test/api/chat/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ room: ROOM, message }),
  });
}

const historyRequest = () => new Request(`http://helm.test/api/chat/history?room=${ROOM}`);

beforeEach(() => {
  vi.stubEnv("ROBOTHOR_ENGINE_URL", "http://engine.test");
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("a workspace send", () => {
  it("sends room_id and no session key, and passes the engine's room stream on unchanged", async () => {
    const fetchMock = engineAnswers({ history: "history", send: "send_stream" });
    const { send } = await routes();
    const res = await send(sendRequest("how did we do?"));
    expect(res.status).toBe(200);
    expect(await res.text()).toBe(engine.send_stream.body);
    const sent = fetchMock.mock.calls.find(([url]) => String(url).includes("/chat/send"))!;
    const body = JSON.parse(String(sent[1]?.body));
    expect(body).toMatchObject({ room_id: ROOM, message: "how did we do?" });
    expect(body).not.toHaveProperty("session_key");
    expect(String(engine.send_stream.body).startsWith("event: room\n")).toBe(true);
  });

  it("answers 409 naming who the agent is answering when the room is busy", async () => {
    engineAnswers({ history: "history", send: "send_busy" });
    const { send } = await routes();
    const res = await send(sendRequest("me too"));
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: "room_busy", answering: "Bob", request_admitted: false });
  });

  it("answers 404 JSON when the caller cannot see the workspace", async () => {
    engineAnswers({ history: "history_not_found", send: "send_not_found" });
    const { send } = await routes();
    const res = await send(sendRequest("hi"));
    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body.request_admitted).toBe(false);
    expect(body.error).not.toBe(ROOM_NOT_CONFIRMED);
  });

  it("answers 403 to a viewer's send", async () => {
    engineAnswers({ history: "history_viewer", send: "send_viewer" });
    const { send } = await routes();
    const res = await send(sendRequest("hi"));
    expect(res.status).toBe(403);
    expect(await res.json()).toMatchObject({ request_admitted: false });
  });
});

describe("a workspace's history", () => {
  it("is shown with each message's author, and no session key", async () => {
    engineAnswers({ history: "history" });
    const { history } = await routes();
    const res = await history(historyRequest());
    expect(res.status).toBe(200);
    const page = await res.json();
    expect(page).not.toHaveProperty("sessionKey");
    const [question, answer] = historyToMessages(page.messages);
    expect(question).toMatchObject({ role: "user", content: "how did we do?", authorUserId: "u-alice", authorName: "Alice" });
    expect(answer).toMatchObject({ role: "assistant", content: "Sales were 1,200." });
    expect(answer.authorUserId).toBeUndefined();
  });

  it("draws an email receipt turn as the engine writes it (E3)", async () => {
    engineAnswers({ history: "history" });
    const { history } = await routes();
    const page = await (await history(historyRequest())).json();
    const messages = historyToMessages(page.messages);
    const receipt = messages.find((m) => m.receipt);
    expect(receipt?.receipt).toMatchObject({ status: "sent", subject: "Checking in", to: ["carol.client@example.org"] });
    expect(receipt?.content).toMatch(/Email sent from assistant@example.com/);
  });

  it("is 404 JSON when the caller cannot see the workspace", async () => {
    engineAnswers({ history: "history_not_found" });
    const { history } = await routes();
    const res = await history(historyRequest());
    expect(res.status).toBe(404);
    expect(await res.json()).toMatchObject({ messages: [] });
  });
});

describe("the engine's edition", () => {
  it("has exactly the bridge's fields, so a period's edition reads like any other", () => {
    const fromEngine = Object.keys(engine.period_existing.body as object).sort();
    const fromBridge = Object.keys(bridge.edition.body as object).sort();
    expect(fromEngine).toEqual(fromBridge);
  });

  it("refuses with the codes and words the period picker shows", () => {
    expect(engine.period_unfinished).toMatchObject({ status: 422, body: { error: "invalid_period" } });
    expect((engine.period_unfinished.body as { message: string }).message).toMatch(/not over yet/);
    // Nothing is capped: six requests in a row by one person each start a run.
    expect(engine.period_many).toEqual({ statuses: [202, 202, 202, 202, 202, 202] });
    expect(engine).not.toHaveProperty("period_cap");
    expect(engine.period_viewer.status).toBe(403);
    expect(engine.period_not_found.status).toBe(404);
    expect(engine.period_started).toMatchObject({ status: 202, body: { status: "building" } });
  });
});
